"""GitLab intent/effect journal and exact task authority, separate from GitHub IDs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from app.connector_errors import ConnectorError
from pydantic import BaseModel, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from sqlalchemy import select

from .contracts import Grant, RunScope
from .effects import effect_id
from .gitlab_request_policy import REVIEW_CAPABILITY, validate_request
from .gitlab_transport import remote_url
from .gitlab_workflow_types import MergeRequest, MRWrite, Project, Status, Target, origin
from .models import Effect, Turn, User, now
from .product_github_models import ProductGitOperation as Operation

if TYPE_CHECKING:
    from .product_connections import ProductConnections


KINDS = {
    "create_branch": "gl_create_branch", "publish_branch": "gl_publish_branch",
    "create_merge_request": "gl_create_mr", "open_merge_request": "gl_open_mr",
    "update_merge_request": "gl_update_mr", "rebase_merge_request": "gl_rebase_mr",
    "close_merge_request": "gl_close_mr", "reopen_merge_request": "gl_reopen_mr",
    "retarget_merge_request": "gl_retarget_mr", "convert_merge_request_to_draft": "gl_draft_mr",
    "request_merge_request_write_access": "gl_write_access",
    "request_mr_lifecycle_access": "gl_lifecycle_access",
    "create_review": "gl_create_review", "submit_review": "gl_submit_review",
    "edit_pending_review": "gl_edit_review", "delete_pending_review": "gl_delete_review",
    "create_stack": "gl_create_stack", "add_to_stack": "gl_add_stack",
}


def parse[T: BaseModel](schema: type[T], value: object) -> T:
    try:
        return schema.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise ToolFailed("GitLab returned an incomplete or different project, MR or operation identity.") from None


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Publications:
    """Use the shared operation/effect tables with provider-specific authority."""

    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant
        self.database = service.database
        self.target = grant.policy["product"]["repository_id"]
        if grant.kind != "gitlab" or type(self.target) is not int or self.target <= 0:
            raise ValueError("A canonical selected GitLab project grant is required.")
        self.origin = origin(grant.policy["product"].get("base_url", "https://gitlab.com"))

    @property
    def allow_loopback_http(self) -> bool:
        return getattr(self.service.connectors.gitlab, "allow_loopback_http", False) is True

    @property
    def prefix(self) -> str:
        return f"/projects/{self.target}"

    @property
    def identity_scope(self) -> dict:
        return {"provider": "gitlab", "origin": self.origin, "project_id": self.target}

    async def authorize(self, db) -> None:
        await self.service.product.identity.authorize_admission(db, self.scope.product_grant)
        actor, turn = await db.get(User, self.scope.actor_id), await db.get(Turn, self.scope.turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise ToolFailed("The GitLab publication requester is unavailable.")
        await self.database._check_product_turn(db, turn, self.scope.product_grant)
        cast(Any, actor).product_grant = self.scope.product_grant
        session = await self.database.execution_session(db, self.scope.session_id, actor)
        if session.active_turn != turn.id or turn.status != "running" or turn.actor_id != actor.id:
            raise ToolFailed("This GitLab publication was stopped or superseded.")
        current = await self.database.scope_grants(db, self.scope, session)
        if self.grant not in current or self.grant.read_only:
            raise ToolFailed("Current GitLab write access is required.")

    def document(self, row: Operation) -> dict:
        return json.loads(self.database.security.unseal(row.document))

    def require_attempt(self, current: Operation, row: Operation) -> None:
        if current.original_effect_id != row.original_effect_id or self.document(current).get("preflight_error"):
            raise ToolFailed("This publication attempt was refused or replaced; it will not dispatch.")

    @asynccontextmanager
    async def attempt(self, ctx: RunContext[RunScope], kind: str, request) -> AsyncIterator[Operation]:
        row = await self.operation(ctx, kind, request)
        try:
            yield row
        except (ToolFailed, ConnectorError) as error:
            if await self.refuse_preflight(row, str(error)):
                raise ToolFailed(
                    f"{error} No repository write was attempted. After correcting the condition, "
                    "retry unchanged inputs with the same request_key in a new tool call."
                ) from None
            raise ToolFailed(
                f"{error} This operation may already have attempted writes. Inspect publication_status "
                "with its original operation/request_key; do not start a replacement publication."
            ) from None

    async def refuse_preflight(self, row: Operation, reason: str) -> bool:
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None or current.original_effect_id != row.original_effect_id:
                return False
            document = self.document(current)
            if document["attempts"] or document["steps"] or document.get("result") or document.get("stack_result"):
                return False
            effect = await db.get(Effect, current.original_effect_id, with_for_update=True)
            if effect is None or effect.state not in {"pending", "not_dispatched"}:
                return False
            document["preflight_error"] = reason
            current.document, current.updated_at = self.database.security.seal(document), now()
            effect.state, effect.settled_at = "not_dispatched", now()
            return True

    async def save(self, row: Operation, **values) -> dict:
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The GitLab operation disappeared.")
            self.require_attempt(current, row)
            document = self.document(current)
            document.update(values)
            current.document, current.updated_at = self.database.security.seal(document), now()
            row.document = current.document
            return document

    async def credentials(self, *, write: bool = False) -> dict:
        await self.service.check(self.scope, self.grant)
        if write and self.grant.read_only:
            raise ToolFailed("This selected GitLab project is read only.")
        credentials = await self.service.credentials(self.scope, self.grant, write=write)
        if origin(credentials["base_url"]) != self.origin:
            raise ToolFailed("The selected GitLab origin changed. Select the connection again.")
        return credentials

    async def check_target(self, request: Target) -> None:
        await self.service.check(self.scope, self.grant)
        if request.repository_id is not None and request.repository_id != self.target:
            raise ToolFailed("This tool belongs to a different selected GitLab project.")
        if request.repository:
            project = await self.project()
            if project.path_with_namespace != request.repository:
                raise ToolFailed("The GitLab project name and immutable selected ID do not match.")

    async def request(self, method: str, path: str, **kwargs) -> Any:
        body = kwargs.get("json")
        validate_request(method, path, self.target, body)
        writing = method != "GET" and body != {"query": REVIEW_CAPABILITY}
        credentials = await self.credentials(write=writing)
        return await self.service.execution.call(
            lambda bound: bound.gitlab.request(
                method, path, token=credentials["access_token"], base_url=self.origin, **kwargs,
            ),
            authorize=lambda: self.service.check(self.scope, self.grant), recheck=not writing,
        )

    async def project(self) -> Project:
        project = parse(Project, await self.request("GET", self.prefix))
        if (
            project.id != self.target or project.http_url_to_repo != remote_url(self.origin, project.path_with_namespace)
            or project.web_url.rstrip("/") != self.origin + "/" + project.path_with_namespace
        ):
            raise ToolFailed("GitLab project identity or its verified remote origin changed.")
        return project

    def operation_id(self, kind: str, key: str) -> str:
        return digest({**self.identity_scope, "session": self.scope.session_id, "kind": KINDS[kind], "key": key})

    def ours(self, row: Operation) -> bool:
        return (
            row.repository_id == self.target and row.connection_id == self.grant.id
            and row.connection_revision == self.grant.revision
            and self.document(row).get("provider_scope") == self.identity_scope
        )

    async def operation(self, ctx: RunContext[RunScope], kind: str, request) -> Operation:
        if not ctx.tool_call_id:
            raise ToolFailed("GitLab publication needs its durable native tool reservation.")
        await self.check_target(request)
        name = f"gitlab_{self.grant.id[:8]}_{kind}"
        effect = effect_id(ctx, ToolCallPart(name, {}, tool_call_id=ctx.tool_call_id))
        identity, fingerprint = self.operation_id(kind, request.request_key), digest(request.model_dump(mode="json"))
        async with self.database.sessions.begin() as db:
            await self.authorize(db)
            receipt = await db.get(Effect, effect)
            if receipt is None or (
                receipt.actor_id != self.scope.actor_id or receipt.session_id != self.scope.session_id
                or receipt.tool_name != name or receipt.state not in {"pending", "confirmed"}
            ):
                raise ToolFailed("GitLab publication requires its current native effect reservation.")
            row = await db.get(Operation, identity, with_for_update=True)
            if row:
                if not self.ours(row) or row.actor_id != self.scope.actor_id or row.fingerprint != fingerprint:
                    raise ToolFailed("This request key belongs to different GitLab intent or authority.")
                document = self.document(row)
                previous = await db.get(Effect, row.original_effect_id, with_for_update=True)
                if (
                    previous is not None and previous.state in {"not_dispatched", "failed"}
                    and receipt.state == "pending" and not document["attempts"] and not document["steps"]
                    and not document.get("result") and effect != row.original_effect_id
                ):
                    row.original_effect_id, row.turn_id = effect, self.scope.turn_id
                    document.pop("preflight_error", None)
                    row.document = self.database.security.seal(document)
                return row
            row = Operation(
                id=identity, session_id=self.scope.session_id, turn_id=self.scope.turn_id,
                actor_id=self.scope.actor_id, connection_id=self.grant.id, connection_revision=self.grant.revision,
                repository_id=self.target, kind=KINDS[kind], request_key=request.request_key,
                fingerprint=fingerprint, original_effect_id=effect,
                document=self.database.security.seal({
                    "provider_scope": self.identity_scope, "operation": kind, "steps": {}, "attempts": [],
                }),
            )
            db.add(row)
            await db.flush()
            return row

    async def freeze(self, row: Operation, **intent) -> dict:
        async with self.database.sessions.begin() as db:
            await self.authorize(db)
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("GitLab operation reservation disappeared.")
            self.require_attempt(current, row)
            document = self.document(current)
            if "intent" in document and document["intent"] != intent:
                raise ToolFailed("The frozen GitLab publication inputs changed.")
            document["intent"] = intent
            current.document = self.database.security.seal(document)
            row.document = current.document
            return document

    async def rows(self, kinds: list[str]) -> list[Operation]:
        async with self.database.sessions() as db:
            rows = list(await db.scalars(select(Operation).where(
                Operation.repository_id == self.target, Operation.connection_id == self.grant.id,
                Operation.connection_revision == self.grant.revision,
                Operation.kind.in_([KINDS[kind] for kind in kinds]),
            )))
        return [row for row in rows if self.ours(row)]

    async def authority(
        self, number: int, *, head: str, base: str, branch_write: bool = False, lifecycle: bool = False,
    ) -> Operation:
        kinds = ["create_merge_request", "open_merge_request",
                 "request_mr_lifecycle_access" if lifecycle else "request_merge_request_write_access"]
        for row in reversed(await self.rows(kinds)):
            document = self.document(row)
            value = document.get("result")
            if not value or document.get("revoked"):
                continue
            mr = parse(MergeRequest, value)
            if mr.iid != number or (document.get("authorized_base") or mr.target_branch) != base:
                continue
            if row.kind in {"gl_write_access", "gl_lifecycle_access"}:
                if (
                    row.actor_id != self.scope.actor_id or row.session_id != self.scope.session_id
                    or row.turn_id != self.scope.turn_id or (not lifecycle and mr.sha != head)
                ):
                    continue
            elif branch_write:
                from .product_gitlab_workflows import Workflows
                if not await Workflows(self).branch_owned(mr.source_branch, head):
                    continue
            return row
        required = "request_mr_lifecycle_access" if lifecycle else "request_merge_request_write_access"
        raise ToolFailed(f"This external MR requires explicit requester approval through {required}.")

    async def bind(self, row: Operation, authority: Operation, mr: MergeRequest, *, lifecycle: bool = False) -> None:
        document = self.document(row)
        scopes = dict(document.get("authorities", {}))
        value = {"id": authority.id, "number": mr.iid, "head": mr.sha, "base": mr.target_branch,
                 "branch": mr.source_branch, "lifecycle": lifecycle}
        if str(mr.iid) in scopes and scopes[str(mr.iid)] != value:
            raise ToolFailed("MR dispatch authority changed within this operation.")
        scopes[str(mr.iid)] = value
        await self.save(row, authorities=scopes)

    async def check_authorities(self, db, row: Operation, document: dict) -> None:
        if row.kind not in {"gl_create_mr", "gl_open_mr", "gl_create_branch", "gl_publish_branch"} and not document.get("authorities"):
            raise ToolFailed("The operation has no admitted MR authority.")
        for scope in sorted(document.get("authorities", {}).values(), key=lambda item: item["id"]):
            authority = await db.get(Operation, scope["id"], with_for_update=True)
            if authority is None or not self.ours(authority):
                raise ToolFailed("The admitted MR grant disappeared.")
            saved = self.document(authority)
            mr = parse(MergeRequest, saved.get("result"))
            if (
                saved.get("revoked") or mr.iid != scope["number"] or mr.source_branch != scope["branch"]
                or (saved.get("authorized_base") or mr.target_branch) != scope["base"]
                or (authority.kind == "gl_write_access" and mr.sha != scope["head"])
                or (authority.kind in {"gl_write_access", "gl_lifecycle_access"} and (
                    authority.actor_id != self.scope.actor_id or authority.session_id != self.scope.session_id
                    or authority.turn_id != self.scope.turn_id
                    or (authority.kind == "gl_lifecycle_access") != scope["lifecycle"]
                ))
            ):
                raise ToolFailed("The admitted MR grant was revoked or changed before dispatch.")

    async def dispatch(self, row: Operation, key: str, action: Callable[[], Awaitable[Any]]) -> Any:
        async with self.database.sessions.begin() as db:
            await self.authorize(db)
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The GitLab reservation disappeared.")
            self.require_attempt(current, row)
            document = self.document(current)
            if key in document["steps"]:
                return document["steps"][key]["response"]
            if key in document["attempts"]:
                raise ToolFailed(f"GitLab {key} has an uncertain receipt and will never be replayed.")
            await self.check_authorities(db, row, document)
            document["attempts"].append(key)
            current.document, current.updated_at = self.database.security.seal(document), now()
            row.document = current.document
        response = await action()
        # Record received evidence even when scope was revoked during the request.
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The GitLab reservation disappeared after dispatch.")
            document = self.document(current)
            document["steps"][key] = {"acknowledged": True, "response": response}
            current.document, current.updated_at = self.database.security.seal(document), now()
            row.document = current.document
        return response

    async def step(self, row: Operation, key: str, method: str, path: str, body: dict) -> Any:
        validate_request(method, path, self.target, body)
        return await self.dispatch(row, key, lambda: self.request(method, path, json=body))

    async def finish(self, row: Operation, result: dict) -> dict:
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            effect = await db.get(Effect, row.original_effect_id, with_for_update=True)
            if current is None or effect is None or not self.ours(current):
                raise ToolFailed("GitLab publication evidence disappeared.")
            self.require_attempt(current, row)
            document = self.document(current)
            document["result"] = result
            current.document, current.updated_at = self.database.security.seal(document), now()
            effect.state, effect.settled_at = "confirmed", effect.settled_at or now()
            row.document = current.document
        if row.kind in {"gl_create_mr", "gl_open_mr"}:
            from .pr_publications import project_gitlab_creation
            await project_gitlab_creation(self.database, self.scope, row.id)
        return result

    async def approve(self, ctx: RunContext[RunScope], request: MRWrite, *, lifecycle: bool = False) -> dict:
        if (
            not ctx.tool_call_approved or ctx.run_id != self.scope.root_run_id
            or self.scope.delegation_generated or self.scope.surface not in {"web", "slack"}
        ):
            raise ToolFailed("Only explicit approval in the requester's direct chat can grant GitLab MR access.")
        kind = "request_mr_lifecycle_access" if lifecycle else "request_merge_request_write_access"
        row = await self.operation(ctx, kind, request)
        if row.turn_id != self.scope.turn_id:
            raise ToolFailed("The approval belongs to an earlier task; request fresh consent.")
        from .product_gitlab_workflows import Workflows
        mr = await Workflows(self).get_mr(request.number, request.head_sha, request.base_branch, closed=lifecycle)
        await self.finish(row, mr.model_dump(mode="json"))
        return {"status": "approved", **self.identity_scope, "number": mr.iid, "head": mr.sha,
                "scope": "task_mr_lifecycle" if lifecycle else "task_exact_head", "url": mr.web_url}

    async def status(self, request: Status) -> dict:
        await self.check_target(request)
        async with self.database.sessions() as db:
            row = await db.get(Operation, self.operation_id(request.operation, request.request_key))
        if row is None or not self.ours(row) or row.actor_id != self.scope.actor_id:
            return {"status": "not_found"}
        document = self.document(row)
        if document.get("result"):
            await self.finish(row, document["result"])
        return {
            "status": "confirmed" if document.get("result") else
                      "not_dispatched" if document.get("preflight_error") else "incomplete",
            **self.identity_scope, "result": document.get("result"),
            "error": document.get("preflight_error"), "attempted_steps": document["attempts"],
            "confirmed_steps": list(document["steps"]),
            "uncertain_steps": [key for key in document["attempts"] if key not in document["steps"]],
            "branch": document.get("intent", {}).get("branch"),
            "review_id": document.get("review_id"),
        }
