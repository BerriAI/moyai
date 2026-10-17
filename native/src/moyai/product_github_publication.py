"""Canonical publication schemas and safeguards on native intent/effect receipts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.db import database as source_database
from app.github import Publish as LegacyPublish
from app.github import Update, workflow_changes
from app.github_coding import PRWrite
from app.github_write_access import GitHubWriteAccess
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select

from .contracts import Grant, RunScope
from .effects import effect_id
from .github_request_policy import validate_request
from .github_workflow_types import (
    AuthorityRef,
    DispatchAuthority,
    LifecycleAccess,
    PublicationStatus,
    Publish,
    PublishBranch,
    PullRequest,
    Rebase,
)
from .models import Effect, Turn, User, now
from .pr_publications import creation_receipt, persist_publication
from .product_github_models import ProductGitOperation as Operation

if TYPE_CHECKING:
    from .product_connections import ProductConnections

SHA = re.compile(r"^[0-9a-f]{40}$")
READ = {"annotations": {"readOnlyHint": True}}
WRITE = {"annotations": {"readOnlyHint": False}}
AUTHORITIES = TypeAdapter(dict[str, DispatchAuthority])


class Publications:
    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant
        self.database = service.database
        self.target = grant.policy["product"]["repository_id"]

    async def authorize(self, db) -> None:
        await self.service.product.identity.authorize_admission(db, self.scope.product_grant)
        actor, turn = await db.get(User, self.scope.actor_id), await db.get(Turn, self.scope.turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise ToolFailed("The publication requester is unavailable.")
        await self.database._check_product_turn(db, turn, self.scope.product_grant)
        session = await self.database.allowed_session(db, self.scope.session_id, actor)
        if session.active_turn != turn.id or turn.status != "running" or turn.actor_id != actor.id:
            raise ToolFailed("This publication was stopped or superseded.")
        current = await self.database.resolve_grants(
            db, actor.id, [item.id for item in self.scope.grants],
            shared_session=session.visibility == "shared", surface=session.surface,
        )
        if self.grant not in current or self.grant.read_only:
            raise ToolFailed("Current GitHub write access is required.")

    async def check_target(self, request) -> None:
        target = await source_database(
            self.service.connectors.github.target, request.repository_id or request.repository,
        )
        if target != self.target:
            raise ToolFailed("This tool is scoped to a different selected repository.")

    async def request(self, method: str, path: str, token: str, **kwargs) -> Any:
        validate_request(method, path, kwargs.get("json"))
        return await self.service.execution.call(
            lambda bound: bound.github.request(method, path, token=token, **kwargs),
            authorize=lambda: self.service.check(self.scope, self.grant), recheck=method == "GET",
        )

    async def token(self, *, workflows: bool = False) -> str:
        await self.service.check(self.scope, self.grant)
        if self.grant.read_only:
            raise ToolFailed("This selected GitHub connection is read only.")
        token = await self.service.execution.call(lambda bound: bound.github.installation_token(
            credentials=bound.github.saved_credentials(), repository=self.target, write=True, workflows=workflows,
        ))
        await self.service.check(self.scope, self.grant)
        return token

    def document(self, row: Operation) -> dict:
        return json.loads(self.database.security.unseal(row.document))

    async def operation(self, ctx: RunContext[RunScope], kind: str, request) -> Operation:
        if not ctx.tool_call_id:
            raise ToolFailed("Publication needs a native tool identity.")
        await self.check_target(request)
        name = f"github_{self.grant.id[:8]}_{kind}"
        stored_kind = {
            "request_pull_request_write_access": "pr_write_access",
            "request_pr_lifecycle_access": "pr_lifecycle_access",
        }.get(kind, kind)
        effect = effect_id(ctx, ToolCallPart(name, {}, tool_call_id=ctx.tool_call_id))
        identity = hashlib.sha256(f"{self.scope.session_id}:{stored_kind}:{request.request_key}".encode()).hexdigest()
        arguments = request.model_dump(mode="json")
        if kind == "create_pull_request":
            # Omitted branch overrides retain the historical request-key identity.
            arguments = {key: value for key, value in arguments.items()
                         if key not in {"base_branch", "head_branch"} or value is not None}
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        async with self.database.sessions.begin() as db:
            await self.authorize(db)
            receipt = await db.get(Effect, effect)
            if (
                receipt is None or receipt.session_id != self.scope.session_id or receipt.actor_id != self.scope.actor_id
                or receipt.tool_name != name or receipt.state not in {"pending", "confirmed"}
            ):
                raise ToolFailed("Publication requires its current durable native reservation.")
            row = await db.get(Operation, identity, with_for_update=True)
            if row:
                if (
                    row.fingerprint != fingerprint or row.actor_id != self.scope.actor_id
                    or row.connection_id != self.grant.id or row.connection_revision != self.grant.revision
                    or row.repository_id != self.target
                ):
                    raise ToolFailed("This request key belongs to different intent or GitHub authority.")
                return row
            row = Operation(
                id=identity, session_id=self.scope.session_id, turn_id=self.scope.turn_id,
                actor_id=self.scope.actor_id, connection_id=self.grant.id, connection_revision=self.grant.revision,
                repository_id=self.target, kind=stored_kind, request_key=request.request_key, fingerprint=fingerprint,
                original_effect_id=effect, document=self.database.security.seal({
                    "steps": {}, "attempts": [],
                    **({"number": request.number, "base_sha": request.base_sha} if kind == "update_pull_request" else {}),
                }),
            )
            db.add(row)
            await db.flush()
            return row

    async def save(self, row: Operation, **values) -> dict:
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            assert current is not None
            document = self.document(current)
            document.update(values)
            current.document, current.updated_at = self.database.security.seal(document), now()
            row.document = current.document
            return document

    async def bind_authority(
        self, row: Operation, authority: AuthorityRef, *, number: int, head: str, base: str, lifecycle: bool = False,
    ) -> None:
        scope = DispatchAuthority(
            **authority.model_dump(), number=number, head=head, base=base, lifecycle=lifecycle,
        ).model_dump(mode="json")
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None:
                raise ToolFailed("The publication reservation disappeared.")
            document = self.document(current)
            scopes = document.setdefault("required_authorities", {})
            previous = scopes.get(str(number))
            if previous is not None and previous != scope:
                raise ToolFailed("The PR authority changed within this operation.")
            scopes[str(number)] = scope
            current.document = self.database.security.seal(document)
            row.document = current.document

    async def finish(self, row: Operation, result: dict) -> dict:
        original, receipt = None, None
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            effect = await db.get(Effect, row.original_effect_id, with_for_update=True)
            original_turn = await db.get(Turn, row.turn_id)
            if current is None or effect is None or original_turn is None:
                raise ToolFailed("The original publication evidence is unavailable.")
            confirmed = effect.settled_at or now()
            if row.kind in {"create_pull_request", "open_pull_request"}:
                original = replace(self.scope, turn_id=row.turn_id, model=original_turn.model)
                name = f"github_{self.grant.id[:8]}_{row.kind}"
                base = result.get("base")
                repository = (base.get("repo") or {}).get("full_name", "") if isinstance(base, dict) else ""
                owner, _, repo = repository.partition("/") if isinstance(repository, str) else ("", "", "")
                receipt = creation_receipt(original, name, {"owner": owner, "repo": repo}, result, confirmed)
                if receipt is None or receipt.repository_id != self.target:
                    raise ToolFailed("GitHub returned no verifiable creation receipt.")
            document = self.document(current)
            document["result"] = result
            effect.state, effect.settled_at = "confirmed", confirmed
            current.document, current.updated_at = self.database.security.seal(document), now()
        if original and receipt:
            await persist_publication(self.database, original, row.original_effect_id, receipt)
        return result

    async def step(self, row: Operation, key: str, method: str, path: str, token: str, body: dict,
                   *, api_version: str = "2022-11-28", include_status: bool = False) -> dict:
        if row.kind == "update_pull_request":
            document = self.document(row)
            await self.authority(document["number"], base_sha=document["base_sha"])
        async with self.database.sessions.begin() as db:
            await self.authorize(db)
            current = await db.get(Operation, row.id, with_for_update=True)
            assert current is not None
            document = self.document(current)
            if key in document["steps"]:
                return document["steps"][key]
            if key in document["attempts"]:
                raise ToolFailed(f"GitHub {key} has an unconfirmed receipt. Inspect it; this write will not be repeated.")
            if row.kind not in {"create_pull_request", "create_branch", "publish_branch", "open_pull_request"}:
                try:
                    scopes = AUTHORITIES.validate_python(document.get("required_authorities"))
                except ValidationError:
                    raise ToolFailed("The operation has no valid admitted PR authority.") from None
                if not scopes:
                    raise ToolFailed("The operation has no admitted PR authority.")
                for scope in sorted(scopes.values(), key=lambda item: item.id or ""):
                    if scope.legacy_owned:
                        continue
                    if scope.id is None:
                        raise ToolFailed("The PR authority is unavailable.")
                    authority = await db.get(Operation, scope.id, with_for_update=True)
                    if authority is None:
                        raise ToolFailed("The PR grant is unavailable.")
                    saved = self.document(authority)
                    receipt = saved.get("result", {})
                    required_grant = "pr_lifecycle_access" if scope.lifecycle else "pr_write_access"
                    if (
                        authority.connection_id != row.connection_id
                        or authority.connection_revision != row.connection_revision
                        or authority.repository_id != row.repository_id
                        or receipt.get("number") != scope.number
                        or authority.kind not in {"create_pull_request", "open_pull_request", required_grant}
                        or saved.get("revoked")
                        or (saved.get("authorized_base") or receipt.get("base", {}).get("ref")) != scope.base
                        or (authority.kind == "pr_write_access" and receipt.get("head", {}).get("sha") != scope.head)
                        or (authority.kind in {"pr_lifecycle_access", "pr_write_access"} and (
                            authority.actor_id != row.actor_id or authority.session_id != row.session_id
                            or authority.turn_id != self.scope.turn_id
                        ))
                    ):
                        raise ToolFailed("The PR grant was revoked or changed before dispatch.")
            document["attempts"].append(key)
            current.document, current.updated_at = self.database.security.seal(document), now()
        value = await self.request(method, path, token, json=body, api_version=api_version, include_status=include_status)
        if not isinstance(value, dict):
            raise ToolFailed("GitHub returned no verifiable operation receipt.")
        # A received provider result is evidence even after Stop/revocation.
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            assert current is not None
            document = self.document(current)
            document["steps"][key] = value
            current.document, current.updated_at = self.database.security.seal(document), now()
            row.document = current.document
        return value

    async def commit(self, row: Operation, request: Publish | Update | PublishBranch | Rebase, token: str) -> str:
        prefix = f"/repositories/{self.target}"
        base = await self.request("GET", prefix + "/git/commits/" + request.base_sha, token)
        tree = (base.get("tree") or {}).get("sha") if isinstance(base, dict) else None
        if not isinstance(tree, str) or not SHA.fullmatch(tree):
            raise ToolFailed("GitHub did not confirm the requested base tree.")
        await self.service.execution.call(
            lambda bound: bound.github.validate_tree_paths(token, tree, request.files, self.target),
            authorize=lambda: self.service.check(self.scope, self.grant),
        )
        entries = []
        for index, change in enumerate(request.files):
            sha = None
            if change.content is not None:
                value = await self.step(row, f"blob-{index}", "POST", prefix + "/git/blobs", token, {
                    "content": change.content, "encoding": change.encoding,
                })
                sha = value.get("sha")
                if not isinstance(sha, str) or not SHA.fullmatch(sha):
                    raise ToolFailed("GitHub did not confirm a file object.")
            entries.append({"path": change.path, "mode": "100755" if change.executable else "100644", "type": "blob", "sha": sha})
        value = await self.step(row, "tree", "POST", prefix + "/git/trees", token, {"base_tree": tree, "tree": entries})
        tree = value.get("sha")
        if not isinstance(tree, str) or not SHA.fullmatch(tree):
            raise ToolFailed("GitHub did not confirm the proposed tree.")
        value = await self.step(row, "commit", "POST", prefix + "/git/commits", token, {
            "message": request.title, "tree": tree, "parents": [request.base_sha],
        })
        sha = value.get("sha")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ToolFailed("GitHub did not confirm the proposed commit.")
        return sha

    def verified_pr(self, value: dict, branch: str, base: str, commit: str) -> dict:
        from .product_github_workflows import parse
        pr = parse(PullRequest, value)
        if (
            pr.draft or pr.head.ref != branch or pr.head.sha != commit or pr.head.repo.id != self.target
            or pr.base.ref != base or pr.base.repo.id != self.target
        ):
            raise ToolFailed("GitHub did not confirm the exact ready-for-review PR identity.")
        return value

    async def create(self, ctx: RunContext[RunScope], request: Publish | LegacyPublish) -> dict:
        from .product_github_workflows import Workflows
        return await Workflows(self).create(ctx, Publish.model_validate(request.model_dump()))

    async def authority(self, number: int, *, base_sha: str | None = None,
                        branch_write: bool = True, lifecycle: bool = False) -> dict:
        async with self.database.sessions() as db:
            rows = list(await db.scalars(select(Operation).where(
                Operation.repository_id == self.target, Operation.connection_id == self.grant.id,
                Operation.connection_revision == self.grant.revision,
                Operation.kind.in_(["create_pull_request", "open_pull_request", "pr_write_access", "pr_lifecycle_access"]),
            )))
        for row in sorted(rows, key=lambda value: (
            value.kind in {"pr_write_access", "pr_lifecycle_access"}, value.created_at,
        ), reverse=True):
            document = self.document(row)
            value = document.get("result")
            if value and value.get("number") == number:
                if row.kind == "pr_lifecycle_access" and not lifecycle:
                    continue
                if row.kind == "pr_write_access" and lifecycle:
                    continue
                if row.kind in {"create_pull_request", "open_pull_request"} and branch_write:
                    from .product_github_workflows import Workflows
                    if not await Workflows(self).branch_owned(value["head"]["ref"], base_sha):
                        continue
                if row.kind in {"pr_write_access", "pr_lifecycle_access"} and (
                    row.actor_id != self.scope.actor_id or row.session_id != self.scope.session_id
                    or row.turn_id != self.scope.turn_id or document.get("revoked")
                    or (row.kind == "pr_write_access" and base_sha is not None
                        and value.get("head", {}).get("sha") != base_sha)
                ):
                    continue
                return {**value, "_authority_id": row.id, "base": {**value.get("base", {}),
                        **({"ref": document["authorized_base"]} if document.get("authorized_base") else {})}}
        version = await source_database(self.service.connectors.github.connection_version)
        rows = await source_database(self.service.store.rows,
            "SELECT * FROM github_publications WHERE connection_version=? AND result!=?", (version, ""))
        for row in rows:
            try:
                value = json.loads(row["result"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get("repository_id") == self.target and value.get("number") == number:
                return {"number": number, "_legacy_owned": True,
                        "head": {"ref": row["branch"], "repo": {"id": self.target}},
                        "base": {"repo": {"id": self.target}}}
        if lifecycle:
            raise ToolFailed("This external PR needs request_pr_lifecycle_access approval for metadata actions.")
        raise ToolFailed("This external PR needs requester approval for its exact head in the current task.")

    async def approve_lifecycle(self, ctx: RunContext[RunScope], request: LifecycleAccess) -> dict:
        from .product_github_workflows import parse
        if not ctx.tool_call_approved or ctx.run_id != self.scope.turn_id or self.scope.surface not in {"web", "slack"}:
            raise ToolFailed("Only explicit requester approval may grant PR lifecycle access.")
        row = await self.operation(ctx, "request_pr_lifecycle_access", request)
        if row.turn_id != self.scope.turn_id:
            raise ToolFailed("That approval belongs to an earlier task. Use a fresh request key.")
        token = await self.token()
        value = await self.request("GET", f"/repositories/{self.target}/pulls/{request.number}", token)
        pr = parse(PullRequest, value)
        if (pr.number != request.number or pr.base.repo.id != self.target or pr.head.repo.id != self.target
                or pr.head.sha != request.head_sha or pr.merged or pr.merged_at):
            raise ToolFailed("The unmerged PR identity changed before consent.")
        await self.finish(row, value)
        return {"status": "approved", "number": request.number, "scope": "task_pr_lifecycle",
                "url": pr.html_url}

    async def update(self, ctx: RunContext[RunScope], request: Update) -> dict:
        row = await self.operation(ctx, "update_pull_request", request)
        saved = self.document(row).get("result")
        if saved:
            return await self.finish(row, saved)
        document = self.document(row)
        acknowledgement = document["steps"].get("advance-branch")
        if acknowledgement:
            from .github_workflow_types import GitReference
            from .product_github_workflows import parse
            acknowledged = parse(GitReference, acknowledgement)
            commit = document["steps"]["commit"]["sha"]
            value = document.get("update_scope")
            if value is None:
                token = await self.token()
                value = await self.request("GET", f"/repositories/{self.target}/pulls/{request.number}", token)
            pr = parse(PullRequest, value)
            if (
                acknowledged.object.sha != commit or acknowledged.ref != "refs/heads/" + pr.head.ref
                or pr.number != request.number or pr.head.repo.id != self.target
            ):
                raise ToolFailed("The saved update acknowledgment does not match the original PR.")
            return await self.finish(row, {
                "number": pr.number, "html_url": pr.html_url, "commit": commit, "branch": pr.head.ref,
                "repository_id": self.target, "state": pr.state, "draft": pr.draft,
            })
        owned = await self.authority(request.number, base_sha=request.base_sha)
        token = await self.token(workflows=workflow_changes(request))
        prefix = f"/repositories/{self.target}"
        pr = await self.request("GET", prefix + f"/pulls/{request.number}", token)
        target, branch, base = GitHubWriteAccess.pr_identity(pr, self.target, request.number)
        if target != self.target or branch != owned["head"]["ref"] or (
            owned.get("base", {}).get("ref") and owned["base"]["ref"] != base
        ):
            raise ToolFailed("The approved PR no longer points to its original selected-repository branch.")
        await self.bind_authority(
            row, AuthorityRef.model_validate(owned), number=request.number, head=request.base_sha, base=base,
        )
        document = self.document(row)
        commit = (document["steps"].get("commit") or {}).get("sha")
        if pr["head"]["sha"] not in {request.base_sha, commit}:
            raise ToolFailed("The PR head changed. Read its current revision before preparing a new update.")
        await self.save(row, update_scope=pr)
        commit = commit or await self.commit(row, request, token)
        path = prefix + "/git/refs/heads/" + quote(branch, safe="/")
        current = await self.request("GET", prefix + "/git/ref/heads/" + quote(branch, safe="/"), token)
        sha = (current.get("object") or {}).get("sha")
        if sha not in {request.base_sha, commit}:
            raise ToolFailed("The branch changed externally; it was not overwritten.")
        if sha != commit:
            from .product_github_workflows import Workflows
            await Workflows(self).writable(token, branch, existing=True)
            current = await self.step(row, "advance-branch", "PATCH", path, token, {"sha": commit, "force": False})
            if (current.get("object") or {}).get("sha") != commit:
                raise ToolFailed("GitHub did not confirm the branch update.")
        result = {"number": request.number, "html_url": pr["html_url"], "commit": commit, "branch": branch,
                  "repository_id": self.target, "state": pr["state"], "draft": pr["draft"]}
        return await self.finish(row, result)

    async def approve(self, ctx: RunContext[RunScope], request: PRWrite) -> dict:
        if not ctx.tool_call_approved or ctx.run_id != self.scope.turn_id or self.scope.surface not in {"web", "slack"}:
            raise ToolFailed("Only an explicit native approval in the requester's direct chat may grant PR access.")
        row = await self.operation(ctx, "request_pull_request_write_access", request)
        if row.turn_id != self.scope.turn_id:
            raise ToolFailed("That approval belongs to an earlier task. Use a new request key for fresh consent.")
        token = await self.token()
        pr = await self.request("GET", f"/repositories/{self.target}/pulls/{request.number}", token)
        target, _, _ = GitHubWriteAccess.pr_identity(pr, self.target, request.number)
        if target != self.target or pr["head"]["sha"] != request.head_sha:
            raise ToolFailed("The PR identity or head changed before approval was applied.")
        await self.finish(row, pr)
        return {"status": "approved", "number": request.number, "repository_id": self.target,
                "head": pr["head"]["sha"], "url": pr["html_url"]}

    async def status(self, request: PublicationStatus) -> dict:
        await self.service.check(self.scope, self.grant)
        await self.check_target(request)
        identity = hashlib.sha256(
            f"{self.scope.session_id}:{request.operation}:{request.request_key}".encode()
        ).hexdigest()
        async with self.database.sessions() as db:
            row = await db.get(Operation, identity)
        if (
            row is None or row.connection_id != self.grant.id or row.actor_id != self.scope.actor_id
            or row.connection_revision != self.grant.revision or row.repository_id != self.target
        ):
            return {"status": "not_found"}
        document = self.document(row)
        if document.get("result"):
            await self.finish(row, document["result"])
        return {"status": "confirmed" if document.get("result") else "incomplete",
                "result": document.get("result"), "attempted_steps": document["attempts"],
                "confirmed_steps": list(document["steps"]), "branch": document.get("branch")}


def toolset(service: ProductConnections, scope: RunScope, grant: Grant):
    publisher = Publications(service, scope, grant)
    tools: FunctionToolset[RunScope] = FunctionToolset(id=f"product_github_publication_{grant.id}")
    from .product_github_tools import register
    register(tools, publisher)

    @tools.tool(metadata=READ)
    async def publication_status(ctx: RunContext[RunScope], request: PublicationStatus) -> dict:
        """Inspect a saved receipt without repeating an uncertain write.

        Set operation to the original unprefixed tool name and retain its request_key.
        Follow-up publishes use update_pull_request; an incomplete receipt is not permission to start a new write.
        """
        return await publisher.status(request)

    if not grant.read_only:
        @tools.tool(metadata=WRITE, sequential=True)
        async def request_pr_lifecycle_access(
            ctx: RunContext[RunScope], request: LifecycleAccess,
        ) -> dict:
            """Ask the requester to allow this PR's metadata changes for the current task.

            Allows close, reopen, retarget and explicit draft conversion even if new commits
            arrive after approval. This never grants branch writes, review approval or merging.
            Supply the current head for the consent display; retargeting revokes this grant.
            """
            return await publisher.approve_lifecycle(ctx, request)

        @tools.tool(metadata=WRITE, sequential=True)
        async def create_pull_request(ctx: RunContext[RunScope], request: Publish) -> dict:
            """Publish bounded, validated file changes from an exact checkout base as a regular PR.

            Uses a dedicated Moyai branch, never overwrites a branch changed elsewhere, and never
            creates a draft. Reuse request_key only for identical intent; uncertain writes are not repeated.
            """
            return await publisher.create(ctx, request)

        @tools.tool(metadata=WRITE, sequential=True)
        async def update_pull_request(ctx: RunContext[RunScope], request: Update) -> dict:
            """Publish follow-up file changes to a confirmed workspace PR or an exact-head PR approval.

            base_sha must be the current PR head. Protected paths, symlinks/submodules and forced
            updates are refused. External PR consent applies only to this task and approved head.
            Never merge, approve a review, or modify an unrelated branch.
            """
            return await publisher.update(ctx, request)

        @tools.tool(metadata=WRITE, sequential=True)
        async def request_pull_request_write_access(ctx: RunContext[RunScope], request: PRWrite) -> dict:
            """Ask the requester to approve one exact external PR head for this task.

            Read the PR first and supply its current head_sha. The SDK pauses before granting access;
            neither model prose, a delegated task nor an automated execution can approve this call.
            Consent expires with this task and cannot authorize changes based on a different head.
            """
            return await publisher.approve(ctx, request)

    async def prepare_approval(_ctx, definitions):
        return [
            replace(tool, kind="unapproved") if tool.name in {
                "request_pull_request_write_access", "request_pr_lifecycle_access",
            } else tool
            for tool in definitions
        ]
    return tools.prepared(prepare_approval).approval_required(
        lambda _ctx, tool, _args: tool.name in {
            "request_pull_request_write_access", "request_pr_lifecycle_access",
        },
    )
