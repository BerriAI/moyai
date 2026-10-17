"""Read-only GitHub PR presentation, authorized by native sessions/connections.

REST contracts: https://docs.github.com/en/rest/pulls/pulls
https://docs.github.com/en/rest/checks/runs
https://docs.github.com/en/rest/commits/statuses
No provider URL, redirect, pagination link, CLI or environment credential is used.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select

from .contracts import ExecutionContext
from .delegation_authority import saved_authority
from .delegation_models import DelegatedSession
from .links import PR_MENTION, LinkInput
from .models import Connection, Message, Session, SessionLink, User

if TYPE_CHECKING:
    from .runtime import Runtime

JSON = dict[str, Any]
Tab = Literal["overview", "files", "discussion", "commits", "checks"]
API = "https://api.github.com"
PUBLIC_TTL = 300
AUTHENTICATED_TTL = 15
MAX_RESPONSE = 3 * 1024 * 1024
MAX_TEXT = 60_000
CONNECT_ERROR = (
    "GitHub could not expose this PR. It may be private, missing, or inaccessible to your account. "
    "Add or update an enabled GitHub connection with repository read access in Connections, "
    "then refresh. Shared sessions require a shared connection."
)


@dataclass(frozen=True)
class PullRequest:
    url: str
    repository: str
    number: int

    @property
    def path(self) -> str:
        return f"/repos/{self.repository}/pulls/{self.number}"


def parse_pr_url(url: str) -> PullRequest:
    try:
        normalized = LinkInput(kind="pull_request", url=url).url
        owner, repo, _, number = urlsplit(normalized).path.strip("/").split("/")
        # LinkInput normalizes attachments; reject path traversal even in legacy rows.
        if owner in {".", ".."} or repo in {".", ".."} or len(number) > 10:
            raise ValueError
        return PullRequest(normalized, f"{owner}/{repo}", int(number))
    except (ValidationError, ValueError):
        raise HTTPException(422, "Use an HTTPS github.com/owner/repository/pull/number URL.") from None


def text(value: Any, limit: int = MAX_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def obj(value: Any) -> JSON:
    return value if isinstance(value, dict) else {}


def rows(value: Any) -> list[JSON]:
    return [item for item in value if isinstance(item, dict)][:100] if isinstance(value, list) else []


def web_link(value: Any) -> str | None:
    """Outgoing browser links only; these are never HTTP request targets."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        return value if (
            parsed.scheme == "https" and parsed.hostname
            and parsed.username is None and parsed.password is None
        ) else None
    except ValueError:
        return None


def identity(value: Any) -> JSON:
    user = obj(value)
    login = text(user.get("login"), 200) or "Unknown"
    return {"login": login, "bot": user.get("type") == "Bot" or login.endswith("[bot]")}


def summary(pr: PullRequest, value: JSON | None = None, error: str | None = None) -> JSON:
    value = value or {}
    state = "merged" if value.get("merged") is True or value.get("merged_at") else value.get("state")
    return {
        "url": pr.url, "title": text(value.get("title"), 1000) or f"{pr.repository} #{pr.number}",
        "repository": pr.repository, "number": pr.number,
        "state": state if state in {"open", "closed", "merged"} else "unknown",
        "draft": value.get("draft") is True, "accessible": error is None,
        **({"error": error} if error else {}),
    }


@dataclass(frozen=True)
class Scope:
    key: str
    # Only encrypted database values are retained in a request scope.
    credentials: tuple[str, ...] = field(repr=False)


@dataclass
class Result:
    value: Any = None
    limited: bool = False
    error: str | None = None
    authenticated: bool = False


class PullRequestService:
    """One instance per router; bounded, process-local TTL cache and single flight.

    Tests can supply a real httpx MockTransport. Production never substitutes an
    environment/gh token for a native Connection, even for a local installation.
    """

    def __init__(
        self, runtime: Runtime, *, transport: httpx.AsyncBaseTransport | None = None,
        ttl: float | None = None, max_entries: int = 256, max_bytes: int = 12 * 1024 * 1024,
    ):
        self.runtime = runtime
        self.transport = transport
        self.ttl, self.max_entries, self.max_bytes = ttl, max_entries, max_bytes
        self.cache: OrderedDict[tuple[str, str], tuple[float, Result, int]] = OrderedDict()
        self.cache_bytes = 0
        self.pending: dict[tuple[str, str], asyncio.Task[Result]] = {}
        self.network = asyncio.Semaphore(6)

    async def contexts(self, user: User, ids: list[str]) -> dict[str, Scope]:
        fresh = await self.runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Sign in to continue.")
        async with self.runtime.database.sessions() as db:
            sessions = (await db.execute(select(Session, DelegatedSession).outerjoin(
                DelegatedSession, DelegatedSession.session_id == Session.id,
            ).where(
                Session.id.in_(ids), Session.deleted.is_(False),
                or_(Session.owner_id == user.id, Session.visibility == "shared"),
            ))).all()
            connections = list(await db.scalars(select(Connection).where(
                Connection.kind == "github", Connection.enabled.is_(True),
                or_(Connection.owner_id == user.id, Connection.shared.is_(True)),
            ).order_by(Connection.id)))
            contexts = {
                session.id: (await saved_authority(self.runtime.database, db, link)).context
                if link is not None else ExecutionContext(session.visibility, session.surface)
                for session, link in sessions
            }
        result = {}
        for session, _ in sessions:
            context = contexts[session.id]
            permitted = []
            for connection in connections:
                if context.surface == "slack" or (context.visibility == "shared" and not connection.shared):
                    continue
                try:
                    policy = json.loads(connection.policy)
                    tools = policy.get("allowed_tools")
                    if tools is not None and "pull_request_read" not in tools:
                        continue
                except (ValueError, AttributeError, TypeError):
                    continue
                permitted.append(connection)
            # Include all authority-bearing fields, not just revision. Rotation,
            # disabling, sharing, owner changes and policy changes fence reuse.
            fingerprint = json.dumps([
                fresh.id, fresh.auth_version, session.owner_id, session.visibility, session.surface,
                [(c.id, c.revision, c.owner_id, c.shared, c.credential, c.policy) for c in permitted],
            ], separators=(",", ":"))
            result[session.id] = Scope(
                hashlib.sha256(fingerprint.encode()).hexdigest(),
                tuple(c.credential for c in permitted),
            )
        return result

    async def discover(self, ids: list[str]) -> dict[str, list[JSON]]:
        """Batch public records only; deliberately no discovery cache or SDK reads."""
        discovered: dict[str, dict[str, JSON]] = {sid: {} for sid in ids}
        if not ids:
            return {}
        async with self.runtime.database.sessions() as db:
            links_ranked = select(
                SessionLink.session_id, SessionLink.url,
                func.row_number().over(
                    partition_by=SessionLink.session_id, order_by=SessionLink.created_at.desc(),
                ).label("position"),
            ).where(SessionLink.session_id.in_(ids), SessionLink.kind == "pull_request").subquery()
            links = (await db.execute(select(links_ranked).where(links_ranked.c.position <= 100))).all()
            ranked = select(
                Message.session_id, Message.content,
                func.row_number().over(
                    partition_by=Message.session_id, order_by=Message.id.desc(),
                ).label("position"),
            ).where(
                Message.session_id.in_(ids), Message.role.in_(["user", "assistant"]),
            ).subquery()
            messages = (await db.execute(select(ranked).where(ranked.c.position <= 100))).all()
        for row in links:
            try:
                pr = parse_pr_url(row.url)
                discovered[row.session_id][pr.url] = {"url": pr.url, "origin": "attached"}
            except HTTPException:
                continue
        for row in messages:
            content = self.runtime.security.unseal(row.content).decode()
            for match in PR_MENTION.finditer(content):
                # Do not treat a malicious longer path or identifier as a PR.
                suffix = content[match.end():match.end() + 1]
                if suffix and suffix not in " \t\r\n?/#)>]}.,;:!\"'":
                    continue
                try:
                    pr = parse_pr_url(match.group())
                except HTTPException:
                    continue
                found = discovered[row.session_id]
                if pr.url not in found and sum(x["origin"] == "mentioned" for x in found.values()) < 30:
                    found[pr.url] = {"url": pr.url, "origin": "mentioned"}
        return {sid: list(found.values()) for sid, found in discovered.items()}

    async def _fetch(self, scope: Scope, path: str, *, prefer_authenticated: bool = False) -> Result:
        # All paths originate in this module. No Link/Location/provider URL is
        # followed, even when GitHub responds with a repository-transfer redirect.
        last_error = CONNECT_ERROR
        async with httpx.AsyncClient(
            transport=self.transport, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(12, connect=5),
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        ) as client:
            # Once this scoped path has needed authentication, use its eligible
            # connections first on revalidation instead of spending an anonymous
            # request on every poll. New/changed authority starts anonymous again.
            credentials = (*scope.credentials, None) if prefer_authenticated else (None, *scope.credentials)
            for credential in credentials:
                headers = {}
                if credential is not None:
                    try:
                        token = self.runtime.security.unseal(credential).decode().strip()
                        if not token or token.lower() == "oauth" or any(ord(c) < 33 or ord(c) > 126 for c in token):
                            continue
                        headers["Authorization"] = f"Bearer {token}"
                    except (ValueError, UnicodeError, InvalidToken):
                        continue
                try:
                    async with self.network, client.stream("GET", API + path, headers=headers) as response:
                        if response.status_code in {401, 403, 404, 429}:
                            last_error = (
                                "GitHub rate limited this request. Wait and refresh, or connect an authorized GitHub account."
                                if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0"
                                else CONNECT_ERROR
                            )
                            continue
                        if 300 <= response.status_code < 400:
                            return Result(error="This repository moved. Attach its current GitHub PR URL and retry.")
                        if response.status_code != 200:
                            return Result(error="GitHub is unavailable. Refresh to try again.")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE:
                                return Result(error="This GitHub response is too large to display. Open the PR in GitHub.")
                        value = json.loads(data)
                        if not isinstance(value, (dict, list)):
                            return Result(error="GitHub returned an incomplete response. Refresh to retry.")
                        return Result(
                            value=value, limited='rel="next"' in response.headers.get("link", ""),
                            authenticated=credential is not None,
                        )
                except (httpx.HTTPError, ValueError, UnicodeError):
                    return Result(error="GitHub could not be read. Refresh to try again.")
        return Result(error=last_error)

    async def get(self, scope: Scope, path: str, refresh: bool = False) -> Result:
        key = (scope.key, path)
        cached = self.cache.get(key)
        if cached and not refresh and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        if key not in self.pending:
            if len(self.pending) >= 128:
                return Result(error="Too many PR reads are in progress. Refresh shortly.")

            async def load() -> Result:
                try:
                    async with asyncio.timeout(45):
                        result = await self._fetch(
                            scope, path, prefer_authenticated=bool(cached and cached[1].authenticated),
                        )
                except TimeoutError:
                    result = Result(error="GitHub took too long to respond. Refresh to try again.")
                size = len(json.dumps(result.value, ensure_ascii=True)) + len(result.error or "")
                if old := self.cache.pop(key, None):
                    self.cache_bytes -= old[2]
                if size <= self.max_bytes and self.max_entries > 0:
                    # Explicit ttl remains an override for both kinds of result,
                    # including tests setting ttl=0 to disable cache reuse.
                    ttl = self.ttl if self.ttl is not None else (
                        AUTHENTICATED_TTL if result.authenticated or result.error else PUBLIC_TTL
                    )
                    self.cache[key] = (time.monotonic() + ttl, result, size)
                    self.cache_bytes += size
                    while len(self.cache) > self.max_entries or self.cache_bytes > self.max_bytes:
                        self.cache_bytes -= self.cache.popitem(last=False)[1][2]
                return result

            task = asyncio.create_task(load())
            self.pending[key] = task
            task.add_done_callback(lambda _task: self.pending.pop(key, None))
        return copy.deepcopy(await asyncio.shield(self.pending[key]))

    async def metadata(self, scope: Scope, pr: PullRequest, refresh: bool = False) -> JSON:
        result = await self.get(scope, pr.path, refresh)
        value = obj(result.value)
        if result.error or value.get("number") != pr.number or value.get("state") not in {"open", "closed"}:
            return summary(pr, error=result.error or "GitHub returned an incomplete PR record. Refresh to retry.")
        body = text(value.get("body"))
        return {
            **summary(pr, value), "description": body,
            "description_limited": len(text(value.get("body"), MAX_RESPONSE)) > len(body),
            "author": identity(value.get("user")),
            "base": text(obj(value.get("base")).get("label"), 500),
            "head": text(obj(value.get("head")).get("label"), 500),
            "head_sha": text(obj(value.get("head")).get("sha"), 64),
            "base_sha": text(obj(value.get("base")).get("sha"), 64),
            "additions": count(value.get("additions")), "deletions": count(value.get("deletions")),
            "changed_files": count(value.get("changed_files")), "commits": count(value.get("commits")),
            "updated_at": text(value.get("updated_at"), 40), "merged_at": text(value.get("merged_at"), 40),
        }

    async def section(self, scope: Scope, pr: PullRequest, detail: JSON, tab: Tab, refresh: bool) -> JSON:
        section_scope = Scope(
            f"{scope.key}:{detail.get('head_sha', '')}:{detail.get('base_sha', '')}", scope.credentials,
        )

        async def get(path: str) -> Result:
            result = await self.get(
                section_scope, path + ("&" if "?" in path else "?") + "per_page=100&page=1", refresh,
            )
            field_name = "check_runs" if path.endswith("/check-runs") else "statuses" if path.endswith("/status") else None
            value = obj(result.value).get(field_name) if field_name else result.value
            if not result.error and not isinstance(value, list):
                return Result(error="GitHub returned an incomplete section. Refresh to retry.")
            return result

        if tab == "files":
            result = await get(pr.path + "/files")
            files = []
            patch_budget = 200_000
            for value in rows(result.value):
                raw_patch = text(value.get("patch"), MAX_RESPONSE)
                patch = "\n".join(raw_patch[:min(40_000, patch_budget)].splitlines()[:1000])
                patch_budget -= len(patch)
                added = sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines())
                removed = sum(line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
                complete = (
                    bool(patch) and len(patch) == len(raw_patch.rstrip("\n"))
                    and added == count(value.get("additions")) and removed == count(value.get("deletions"))
                )
                files.append({
                    "filename": text(value.get("filename"), 2000), "previous_filename": text(value.get("previous_filename"), 2000),
                    "status": text(value.get("status"), 40),
                    "additions": count(value.get("additions")), "deletions": count(value.get("deletions")),
                    "patch": patch, "patch_status": "available" if complete else "partial" if raw_patch else "unavailable",
                })
            # PR files/commits endpoints track a mutable branch. Fence the loaded
            # page against a head/base change during the fetch; never label a mixed
            # revision as the displayed diff.
            current = await self.metadata(scope, pr, True)
            if not current["accessible"] or any(current.get(k) != detail.get(k) for k in ("head_sha", "base_sha")):
                return {"items": [], "limited": True, "error": "The PR changed while loading. Refresh to see its current diff."}
            return {
                "items": files, "limited": result.limited or detail["changed_files"] > len(files),
                "error": result.error,
                "notice": "GitHub may omit binary or large patches. Display is limited to 100 files, "
                "1,000 lines / 40,000 characters per patch and 200,000 patch characters total.",
            }
        if tab == "discussion":
            paths = [
                (f"/repos/{pr.repository}/issues/{pr.number}/comments", "comment"),
                (pr.path + "/reviews", "review"), (pr.path + "/comments", "inline"),
            ]
            results = await asyncio.gather(*(get(path) for path, _ in paths))
            items = []
            for (_, kind), result in zip(paths, results):
                for value in rows(result.value):
                    body = text(value.get("body"))
                    items.append({
                        "id": f"{kind}-{value.get('id')}", "kind": kind,
                        "author": identity(value.get("user")), "body": body,
                        "body_limited": len(text(value.get("body"), MAX_RESPONSE)) > len(body),
                        "state": text(value.get("state"), 80), "path": text(value.get("path"), 2000),
                        "line": value.get("line") or value.get("original_line"),
                        "created_at": text(value.get("submitted_at") or value.get("created_at"), 40),
                        "url": web_link(value.get("html_url")),
                    })
            items.sort(key=lambda item: item["created_at"])
            return {
                "items": items, "limited": any(r.limited for r in results),
                "error": "Some discussion sources are unavailable. " + next(r.error for r in results if r.error)
                if any(r.error for r in results) else None,
            }
        if tab == "commits":
            result = await get(pr.path + "/commits")
            current = await self.metadata(scope, pr, True)
            if not current["accessible"] or current.get("head_sha") != detail.get("head_sha"):
                return {"items": [], "limited": True, "error": "The PR changed while loading. Refresh to see its current commits."}
            return {
                "items": [{
                    "sha": text(value.get("sha"), 64), "message": text(obj(value.get("commit")).get("message")),
                    "author": identity(value.get("author")),
                    "created_at": text(obj(obj(value.get("commit")).get("author")).get("date"), 40),
                    "url": web_link(value.get("html_url")),
                } for value in rows(result.value)],
                "limited": result.limited or detail["commits"] > len(rows(result.value)), "error": result.error,
            }
        sha = detail.get("head_sha", "")
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
            return {"items": [], "limited": False, "error": "GitHub did not provide a valid head revision for checks."}
        checks, statuses = await asyncio.gather(
            get(f"/repos/{pr.repository}/commits/{sha}/check-runs"),
            get(f"/repos/{pr.repository}/commits/{sha}/status"),
        )
        items = []
        for value in rows(obj(checks.value).get("check_runs")):
            output = obj(value.get("output"))
            items.append({
                "id": f"check-{value.get('id')}", "name": text(value.get("name"), 1000),
                "status": text(value.get("conclusion") or value.get("status"), 80) or "unknown",
                "url": web_link(value.get("html_url") or value.get("details_url")),
                "title": text(output.get("title"), 2000), "summary": text(output.get("summary")),
                "text": text(output.get("text")), "annotations": count(output.get("annotations_count")),
                "output_limited": any(len(text(output.get(k), MAX_RESPONSE)) > MAX_TEXT for k in ("summary", "text")),
            })
        for value in rows(obj(statuses.value).get("statuses")):
            items.append({
                "id": f"status-{value.get('id')}", "name": text(value.get("context"), 1000),
                "status": text(value.get("state"), 80) or "unknown",
                "url": web_link(value.get("target_url")), "title": text(value.get("description"), 2000),
                "summary": "", "text": "", "annotations": 0, "output_limited": False,
            })
        return {
            "items": items, "head_sha": sha,
            "limited": checks.limited or statuses.limited
            or count(obj(checks.value).get("total_count")) > len(rows(obj(checks.value).get("check_runs")))
            or count(obj(statuses.value).get("total_count")) > len(rows(obj(statuses.value).get("statuses"))),
            "error": "Some check sources are unavailable. " + (checks.error or statuses.error or "")
            if checks.error or statuses.error else None,
        }

    async def statuses(self, user: User, ids: list[str]) -> dict[str, list[JSON]]:
        contexts = await self.contexts(user, ids)
        discovered = await self.discover(list(contexts))
        # Deduplicate by authority and PR across sessions before creating tasks.
        unique = {(contexts[sid].key, item["url"]): (contexts[sid], item["url"])
                  for sid, items in discovered.items() for item in items}
        values = {}
        # Bounded waves keep requests/cache-flight state bounded for large batches.
        entries = list(unique.items())
        for start in range(0, len(entries), 24):
            chunk = entries[start:start + 24]
            loaded = await asyncio.gather(*(self.metadata(scope, parse_pr_url(url)) for _, (scope, url) in chunk))
            values.update((key, value) for (key, _), value in zip(chunk, loaded))
        current = await self.contexts(user, list(contexts))
        result = {}
        for sid, items in discovered.items():
            if sid not in current:
                continue
            result[sid] = []
            for item in items:
                value = values[(contexts[sid].key, item["url"])]
                if current[sid] != contexts[sid]:
                    value = summary(parse_pr_url(item["url"]), error="Connection access changed. Refresh to retry.")
                result[sid].append({**{k: value[k] for k in (
                    "url", "title", "repository", "number", "state", "draft", "accessible",
                )}, **({"error": value["error"]} if value.get("error") else {}), "origin": item["origin"]})
        return result

    async def detail(self, user: User, session_id: str, url: str, tab: Tab, refresh: bool) -> JSON:
        contexts = await self.contexts(user, [session_id])
        if session_id not in contexts:
            raise HTTPException(404, "Conversation not found.")
        pr, scope = parse_pr_url(url), contexts[session_id]
        detail = await self.metadata(scope, pr, refresh)
        if tab != "overview" and detail["accessible"]:
            detail["section"] = await self.section(scope, pr, detail, tab, refresh)
        current = await self.contexts(user, [session_id])
        if session_id not in current:
            raise HTTPException(404, "Conversation not found.")
        if current[session_id] != scope:
            raise HTTPException(409, "Connection access changed. Refresh to retry.")
        return {**detail, "tab": tab}


class StatusRequest(BaseModel):
    session_ids: list[str] = Field(max_length=100)


def routes(runtime: Runtime, actor, *, service: PullRequestService | None = None) -> APIRouter:
    router = APIRouter()
    service = service or PullRequestService(runtime)

    @router.post("/api/pull-requests/statuses")
    async def statuses(body: StatusRequest, response: Response, user: User = Depends(actor)):
        response.headers["Cache-Control"] = "no-store"
        return await service.statuses(user, list(dict.fromkeys(body.session_ids)))

    @router.get("/api/sessions/{session_id}/pull-request")
    async def detail(
        session_id: str, response: Response, url: str = Query(max_length=2048),
        refresh: bool = False, tab: Tab = "overview", user: User = Depends(actor),
    ):
        response.headers["Cache-Control"] = "no-store"
        return await service.detail(user, session_id, url, tab, refresh)

    return router
