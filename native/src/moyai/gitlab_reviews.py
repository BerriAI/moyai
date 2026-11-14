"""Read-only MR views using selected canonical project grants, never URL credentials."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import select

from .connection_policy import ConnectionPolicy
from .contracts import ExecutionContext, Grant
from .links import gitlab_link
from .models import Session, Turn, User
from .product_identity import ProductGrant

if TYPE_CHECKING:
    from .pull_requests import PullRequest, Result, Scope
    from .runtime import Runtime

ERROR = "This MR requires its current selected GitLab project connection. Check Connections and the chat's account selection, then refresh."
MAX_TEXT = 60_000
SECTIONS = {
    "/diffs": "merge_request_diffs", "/discussions": "merge_request_discussions",
    "/commits": "merge_request_commits", "/pipelines": "merge_request_pipelines",
}


@dataclass(frozen=True)
class ReviewSelection:
    actor_id: str
    auth_version: int
    session_id: str
    context: ExecutionContext
    selections: tuple[str, ...]
    grants: tuple[Grant, ...]
    product_grant: ProductGrant | None = None

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps([
            self.actor_id, self.auth_version, self.session_id,
            self.context.visibility, self.context.surface, self.selections,
            [(g.id, g.revision, g.read_only, g.policy) for g in self.grants],
        ], sort_keys=True).encode()).hexdigest()


async def selection(
    runtime: Runtime, user: User, session: Session, context: ExecutionContext,
) -> ReviewSelection | None:
    service = runtime.product_connections
    if service is None:
        return None
    product_grant = getattr(user, "product_grant", None)
    product = getattr(service, "product", None)
    if product is not None:
        if product_grant is None:
            return None
        await product.identity.require_current(product_grant)
    async with runtime.database.sessions() as db:
        turn = await db.scalar(select(Turn).where(
            Turn.session_id == session.id,
        ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))
    try:
        ids = json.loads(turn.connections) if turn else []
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            return None
        # Exact saved selection only: never ask for every organization GitLab grant.
        grants = await service.resolve_grants(
            user.id, ids, shared_session=context.visibility == "shared", surface=context.surface,
        )
    except (HTTPException, PermissionError, ValueError):
        return None
    selected = tuple(g for g in grants if g.kind == "gitlab" and g.policy.get("product"))
    return ReviewSelection(user.id, user.auth_version, session.id, context, tuple(ids), selected, product_grant)


def project_reference(grant: Grant) -> tuple[str, int]:
    reference = grant.policy.get("product", {})
    origin = reference.get("base_url")
    project_id = reference.get("repository_id")
    if not isinstance(origin, str) or type(project_id) is not int or project_id <= 0:
        raise PermissionError(ERROR)
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise PermissionError(ERROR)
    canonical, _, _ = gitlab_link(origin.rstrip("/") + "/scope/project/-/merge_requests/1")
    return canonical, project_id


def mr_identity(value: Any, project_id: int, iid: int, origin: str, project: str) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        linked = gitlab_link(value.get("web_url", ""))
    except (ValueError, TypeError, AttributeError):
        return False
    return (
        type(value.get("iid")) is int and value["iid"] == iid
        and type(value.get("project_id")) is int and value["project_id"] == project_id
        and type(value.get("id")) is int and value["id"] > 0
        and linked == (origin, project, iid)
        and value.get("state") in {"opened", "closed", "merged", "locked"}
        and isinstance(value.get("draft"), bool)
    )


class GitLabReviews:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.network = asyncio.Semaphore(6)

    async def check(self, expected: ReviewSelection) -> None:
        from .delegation_authority import session_context

        user = await self.runtime.database.user(expected.actor_id)
        if not user.enabled or user.auth_version != expected.auth_version:
            raise PermissionError(ERROR)
        if expected.product_grant is not None:
            product = self.runtime.product_connections
            if product is None:
                raise PermissionError(ERROR)
            await product.product.identity.require_current(expected.product_grant)
            cast(Any, user).product_grant = expected.product_grant
        async with self.runtime.database.sessions() as db:
            session = await db.get(Session, expected.session_id)
            if session is None or session.deleted or (
                session.owner_id != user.id and session.visibility != "shared"
            ):
                raise PermissionError(ERROR)
            context = await session_context(self.runtime.database, db, session)
        current = await selection(self.runtime, user, session, context)
        if current != expected:
            raise PermissionError(ERROR)

    async def read(
        self, scope: Scope, pr: PullRequest, suffix: str = "", *, project_id: int | None = None,
    ) -> Result:
        from .pull_requests import Result

        try:
            async with asyncio.timeout(45):
                return await self._read(scope, pr, suffix, project_id=project_id)
        except TimeoutError:
            return Result(error="GitLab took too long to respond. Refresh to try again.")

    async def _read(
        self, scope: Scope, pr: PullRequest, suffix: str = "", *, project_id: int | None = None,
    ) -> Result:
        from .pull_requests import Result

        selected = scope.gitlab
        service = self.runtime.product_connections
        if selected is None or service is None:
            return Result(error=ERROR)
        try:
            await self.check(selected)
            candidates = []
            for grant in selected.grants:
                origin, identity = project_reference(grant)
                policy = ConnectionPolicy.model_validate(grant.policy)
                if (
                    origin == pr.origin and (project_id is None or project_id == identity)
                    and (not origin.startswith("http:") or
                         getattr(service.connectors.gitlab, "allow_loopback_http", False) is True)
                    and policy.allows("merge_request") and (not suffix or policy.allows(SECTIONS[suffix]))
                ):
                    candidates.append((grant, origin, identity))
            # Project paths are display labels, never permission. Resolve only the IDs
            # already selected for this chat at the configured origin.
            for grant, origin, identity in candidates:
                async def request(
                    path: str, *, params: dict[str, Any] | None = None,
                    grant: Grant = grant, origin: str = origin,
                ) -> Any:
                    await self.check(selected)
                    credentials = await service.read_credentials(grant, lambda: self.check(selected))
                    await self.check(selected)
                    async with self.network:
                        value = await service.execution.call(
                            lambda bound: bound.gitlab.request(
                                "GET", path, token=credentials["access_token"], base_url=origin, params=params,
                            ),
                            authorize=lambda: self.check(selected), recheck=True,
                        )
                    await self.check(selected)
                    return value

                info = await request(f"/projects/{identity}")
                if (
                    not isinstance(info, dict) or type(info.get("id")) is not int or info["id"] != identity
                    or not isinstance(info.get("path_with_namespace"), str)
                ):
                    continue
                repository = info["path_with_namespace"]
                # Attachments must still match the selected project's current URL.
                # Only an encrypted, verified creation receipt can follow a rename
                # using an immutable project ID instead of the stored display path.
                if (
                    (project_id is None and repository != pr.repository)
                    or info.get("web_url") != f"{origin}/{repository}"
                    or gitlab_link(f"{origin}/{repository}/-/merge_requests/{pr.number}")
                    != (origin, repository, pr.number)
                ):
                    continue
                value = await request(
                    f"/projects/{identity}/merge_requests/{pr.number}" + suffix,
                    params={"per_page": 100, "page": 1} if suffix else None,
                )
                if not suffix and not mr_identity(value, identity, pr.number, origin, repository):
                    return Result(error="GitLab returned a different or incomplete MR.", authenticated=True)
                if suffix and not isinstance(value, list):
                    return Result(error="GitLab returned an incomplete review section.", authenticated=True)
                return Result(value=value, limited=isinstance(value, list) and len(value) >= 100, authenticated=True)
        except (ConnectorError, HTTPException, PermissionError, ValueError, KeyError, TimeoutError):
            return Result(error=ERROR, authenticated=True)
        return Result(error=ERROR)

    async def metadata(self, scope: Scope, pr: PullRequest) -> dict[str, Any]:
        from .pull_requests import obj, summary, text

        result = await self.read(scope, pr)
        if result.error:
            return summary(pr, error=result.error)
        value = result.value
        body = text(value.get("description"))
        refs = obj(value.get("diff_refs"))
        changes = value.get("changes_count")
        count = int(changes) if (
            isinstance(changes, str) and len(changes) <= 10 and changes.isascii() and changes.isdecimal()
        ) else None
        return {
            **summary(pr, {"title": value.get("title"), "draft": value["draft"],
                          "state": "open" if value["state"] in {"opened", "locked"} else value["state"]}),
            "description": body, "description_limited": len(text(value.get("description"), 3 * 1024 * 1024)) > len(body),
            "author": self.author(value.get("author")), "base": text(value.get("target_branch"), 500),
            "head": text(value.get("source_branch"), 500), "head_sha": text(value.get("sha"), 64),
            "base_sha": text(refs.get("base_sha"), 64), "changed_files": count,
            "updated_at": text(value.get("updated_at"), 40), "merged_at": text(value.get("merged_at"), 40),
        }

    @staticmethod
    def author(value: Any) -> dict[str, Any]:
        value = value if isinstance(value, dict) else {}
        return {"login": str(value.get("username") or value.get("name") or "Unknown")[:200],
                "bot": value.get("bot") is True}

    async def section(
        self, scope: Scope, pr: PullRequest, detail: dict[str, Any], tab: str,
    ) -> dict[str, Any]:
        from .pull_requests import obj, rows, text, web_link

        suffix = {"files": "/diffs", "discussion": "/discussions", "commits": "/commits", "checks": "/pipelines"}[tab]
        result = await self.read(scope, pr, suffix)
        items: list[dict[str, Any]] = []
        limited = result.limited
        budget = 200_000
        for value in rows(result.value):
            if tab == "files":
                raw = text(value.get("diff"), 3 * 1024 * 1024)
                patch = "\n".join(raw[:min(40_000, budget)].splitlines()[:1000])
                budget -= len(patch)
                complete = bool(patch) and patch == raw.rstrip("\n") and not (
                    value.get("collapsed") or value.get("too_large")
                )
                items.append({
                    "filename": text(value.get("new_path") or value.get("old_path"), 2000),
                    "previous_filename": text(value.get("old_path"), 2000) if value.get("renamed_file") else "",
                    "status": "renamed" if value.get("renamed_file") else "added" if value.get("new_file") else
                              "removed" if value.get("deleted_file") else "modified",
                    "additions": sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines()),
                    "deletions": sum(line.startswith("-") and not line.startswith("---") for line in patch.splitlines()),
                    "patch": patch, "patch_status": "available" if complete else "partial" if raw else "unavailable",
                })
            elif tab == "discussion":
                notes = value.get("notes")
                if not isinstance(notes, list):
                    limited = True
                    continue
                for note in notes:
                    if not isinstance(note, dict) or len(items) >= 100:
                        limited = True
                        continue
                    position = obj(note.get("position"))
                    body = text(note.get("body"))
                    line = position.get("new_line") or position.get("old_line")
                    items.append({
                        "id": f"note-{note.get('id')}", "kind": "inline" if position else "comment",
                        "author": self.author(note.get("author")), "body": body,
                        "body_limited": len(text(note.get("body"), 3 * 1024 * 1024)) > len(body),
                        "state": "RESOLVED" if note.get("resolved") else "",
                        "path": text(position.get("new_path") or position.get("old_path"), 2000),
                        "line": line if type(line) is int and line > 0 else None,
                        "created_at": text(note.get("created_at"), 40),
                        "url": pr.url + f"#note_{note['id']}" if type(note.get("id")) is int else None,
                    })
            elif tab == "commits":
                items.append({
                    "sha": text(value.get("id"), 64), "message": text(value.get("message")),
                    "author": {"login": text(value.get("author_name"), 200) or "Unknown", "bot": False},
                    "created_at": text(value.get("committed_date") or value.get("created_at"), 40),
                    "url": web_link(value.get("web_url")),
                })
            else:
                if value.get("sha") != detail.get("head_sha"):
                    continue
                status = text(value.get("status"), 80)
                items.append({
                    "id": f"pipeline-{value.get('id')}", "name": text(value.get("name"), 1000) or f"Pipeline #{value.get('id')}",
                    "status": "failure" if status == "failed" else status,
                    "url": web_link(value.get("web_url")), "title": text(value.get("ref"), 2000),
                    "summary": "", "text": "", "annotations": 0, "output_limited": False,
                })
        current = await self.metadata(scope, pr)
        if not current["accessible"] or any(
            current.get(key) != detail.get(key) for key in ("head_sha", "base_sha")
        ):
            return {"items": [], "limited": True, "error": "The MR or its access changed while loading. Refresh to retry."}
        response: dict[str, Any] = {"items": items, "limited": limited, "error": result.error}
        if tab == "files":
            response["notice"] = "GitLab may omit large or binary diffs. Counts describe displayed patch lines; up to 100 files and 200,000 patch characters are shown."
        if tab == "checks":
            response["head_sha"] = detail.get("head_sha")
        return response
