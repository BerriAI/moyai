"""Selected GitLab bootstrap: trusted Git transport and credential-free receipts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from app.gitlab_transport import _public_address, normalize_base_url
from app.repository_identity import (
    canonical_origin,
    canonical_repository_url,
    repository_origin,
    repository_provider,
)
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .contracts import Grant, RunScope
from .models import Effect, Session, now
from .product_github_bootstrap import INSTALL, MAX_BUNDLE, git
from .storage import ObjectStore

if TYPE_CHECKING:
    from .product_connections import ProductConnections

SHA = re.compile(r"^[0-9a-f]{40}$")


def valid_branch(value: Any) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 1024 and not value.startswith("-")
        and value != "@" and not value.endswith(".")
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "~^:?*[\\" for c in value)
        and ".." not in value and "@{" not in value
        and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/"))
    )


def project_url(value: str) -> str:
    canonical = canonical_repository_url(value)
    if not canonical or repository_provider(canonical) != "gitlab":
        raise ValueError("Use an exact GitLab repository URL.")
    return canonical


def grant_matches(grant: Grant, origin: str, target: int | None = None) -> bool:
    product = grant.policy.get("product") or {}
    try:
        identity = product.get("repository_id")
        return (
            grant.kind == "gitlab" and type(identity) is int and identity > 0
            and canonical_origin(product.get("base_url", "")) == origin
            and (target is None or identity == target)
        )
    except ValueError:
        return False


def validate_credentials(credentials: dict[str, Any], origin: str, target: int) -> str:
    if (
        canonical_origin(credentials.get("base_url", "")) != origin
        or target not in credentials.get("repository_ids", [])
        or not isinstance(credentials.get("access_token"), str)
        or not credentials["access_token"]
        or len(credentials["access_token"]) > 4096
        or any(ord(c) < 33 or ord(c) > 126 for c in credentials["access_token"])
    ):
        raise PermissionError("The GitLab credential origin or project selection changed.")
    return credentials["access_token"]


def metadata(info: Any, *, origin: str, target: int, requested_url: str = "") -> tuple[str, str]:
    if not isinstance(info, dict) or type(info.get("id")) is not int or info["id"] != target:
        raise ValueError("GitLab did not confirm the selected project ID.")
    name = info.get("path_with_namespace")
    if not isinstance(name, str):
        raise TypeError("GitLab did not confirm the project path.")
    url = project_url(origin + "/" + name)
    if (
        info.get("web_url") is None or project_url(info["web_url"]) != url
        or (requested_url and project_url(requested_url).casefold() != url.casefold())
    ):
        raise ValueError("GitLab did not confirm the selected project origin and path.")
    return name, url


async def git_transport_environment(
    url: str, token: str, *, allow_loopback_http: bool = False,
) -> dict[str, str]:
    """Pin exactly the same DNS/address ceiling as the provider REST adapter."""
    canonical = project_url(url)
    origin = repository_origin(canonical)
    normalize_base_url(origin, allow_loopback_http=allow_loopback_http)
    if (
        not isinstance(token, str) or not token or len(token) > 4096
        or any(ord(c) < 33 or ord(c) > 126 for c in token)
    ):
        raise ValueError("Use a valid selected GitLab credential.")
    parsed = urlsplit(origin)
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        async with asyncio.timeout(10):
            records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
        if parsed.scheme == "http" and allow_loopback_http:
            from ipaddress import ip_address
            permitted = all(ip_address(address).is_loopback for address in addresses)
        else:
            permitted = all(_public_address(address) for address in addresses)
        if not addresses or not permitted:
            raise ValueError
    except (OSError, TimeoutError, ValueError):
        raise ValueError("GitLab Git transport could not resolve a permitted network address.") from None
    # An entry without '+' never expires during this fetch. Preserve the
    # configured hostname for TLS/SNI/Host and pin the socket destination.
    pinned = addresses[0]
    if ":" in pinned:
        pinned = f"[{pinned}]"
    resolve_host = f"[{host}]" if ":" in host else host
    settings = [
        ("credential.helper", ""), ("http.followRedirects", "false"), ("http.proxy", ""),
        ("http.curloptResolve", f"{resolve_host}:{port}:{pinned}"),
        ("http." + origin + "/.extraHeader",
         "Authorization: Basic " + base64.b64encode(("oauth2:" + token).encode()).decode()),
    ]
    environment = {key: os.environ[key] for key in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_COUNT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
    })
    # Older Git builds silently ignore unknown configuration. Refuse transport
    # unless this binary advertises DNS pinning, before attaching credentials.
    capabilities = await git(["help", "--config"], Path(tempfile.gettempdir()), environment)
    if "http.curloptResolve" not in capabilities.splitlines():
        raise ValueError("The trusted worker Git must support pinned HTTP DNS resolution.")
    environment["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


async def fetch_bundle(
    url: str, branch: str, commit: str, token: str, destination: Path, *, allow_loopback_http: bool = False,
) -> bytes:
    """Only Git objects leave this worker; authentication lives in process env."""
    canonical = project_url(url)
    if not valid_branch(branch) or not SHA.fullmatch(commit):
        raise ValueError("GitLab bootstrap requires a verified branch and commit.")
    environment = await git_transport_environment(canonical, token, allow_loopback_http=allow_loopback_http)
    with tempfile.TemporaryDirectory(prefix="moyai-gitlab-source-") as temporary:
        root = Path(temporary)

        def directory_size() -> int:
            size = 0
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass
            return size

        await git(["init", "--bare", "."], root, environment)
        task = asyncio.create_task(git(["fetch", "--no-tags", canonical + ".git", commit], root, environment))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=.5)
                if await asyncio.to_thread(directory_size) > MAX_BUNDLE:
                    raise ValueError("GitLab repository history exceeds the 128 MiB bootstrap budget.")
            await task
            await git(["update-ref", "refs/heads/" + branch, commit], root, environment)
            await git(["bundle", "create", str(destination), "--branches"], root, environment)
            if destination.stat().st_size > MAX_BUNDLE:
                raise ValueError("GitLab repository bundle exceeds the bootstrap budget.")
            return await asyncio.to_thread(destination.read_bytes)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def selected_grant(service: ProductConnections, scope: RunScope, context: dict[str, Any]) -> Grant:
    url = project_url(str(context.get("repo_url") or ""))
    origin = repository_origin(url)
    target = context.get("gitlab_project_id")
    if (
        context.get("repository_provider", "gitlab") != "gitlab"
        or context.get("repository_origin", origin) != origin
        or context.get("github_repository_id") is not None
        or (target is not None and (type(target) is not int or target <= 0))
        or (context.get("repository_id") is not None and (
            type(context["repository_id"]) is not int or context["repository_id"] != target
        ))
    ):
        raise ValueError("The frozen GitLab provider, origin or project identity changed.")
    await service.database.authorize_scope(scope)
    await service.require_scope(scope)
    candidates = [grant for grant in scope.grants if grant_matches(grant, origin, target)]
    if not candidates:
        raise HTTPException(403, "Select the exact GitLab origin and project before repository checkout.")
    for candidate in candidates:
        await service.check(scope, candidate)
    async def configuration(bound):
        return bound.gitlab.normalize_base_url(origin)
    if await service.execution.call(configuration) != origin:
        raise PermissionError("The configured GitLab origin changed.")
    if target is None and len(candidates) > 1:
        # Read safe saved metadata only after confirming the configured origin's
        # current authority. Never try tokens from unrelated origins/projects.
        async def lookup(bound):
            saved = bound.gitlab.saved_credentials()
            if canonical_origin(saved.get("base_url", "")) != origin:
                raise PermissionError("The configured GitLab origin changed.")
            return bound.gitlab.target(urlsplit(url).path.lstrip("/"), saved)

        target = await service.execution.call(lookup, authorize=lambda: service.require_scope(scope))
        candidates = [candidate for candidate in candidates if grant_matches(candidate, origin, target)]
    if len(candidates) != 1:
        raise HTTPException(403, "Choose one exact selected GitLab project.")
    return candidates[0]


async def prepare_repository(
    service: ProductConnections, scope: RunScope, sandbox: Any, context: dict[str, Any],
) -> dict[str, Any]:
    if not context.get("repo_url"):
        return {"prepared": False, "reason": "No bare repository checkout requested."}
    if context.get("environment_snapshot_id") and context.get("environment_build_id"):
        return {"prepared": False, "reason": "Validated environment images use workspace restoration."}
    if context.get("environment_snapshot_id") or context.get("environment_build_id"):
        raise ValueError("A validated environment must be materialized by workspace restoration.")
    if context.get("working_dir") != "/workspace/repo":
        raise ValueError("Use the canonical repository working directory.")
    url = project_url(context["repo_url"])
    origin = repository_origin(url)
    if context.get("commit_sha") is not None and not SHA.fullmatch(str(context["commit_sha"])):
        raise ValueError("Use an exact GitLab commit SHA.")
    grant = await selected_grant(service, scope, context)
    target = grant.policy["product"]["repository_id"]

    async def check():
        await service.check(scope, grant)
        async def policy(bound):
            if bound.gitlab.normalize_base_url(origin) != origin:
                raise PermissionError("The configured GitLab origin changed.")
            return bound.policy("gitlab")["enabled"]
        if not await service.execution.call(policy):
            raise HTTPException(403, "Repository checkout is disabled by current GitLab policy.")

    await check()
    descriptor = {
        "session_id": scope.session_id, "workspace_id": sandbox.object_id, "workspace_scope": scope.workspace_key,
        "repository_provider": "gitlab", "repository_origin": origin, "gitlab_project_id": target,
        "repository_id": target, "connection_id": grant.id, "connection_revision": grant.revision,
        "working_dir": "/workspace/repo", "requested_url": url, "access": "selected_connection",
        "requested_commit": context.get("commit_sha"),
    }
    fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    identity = hashlib.sha256(("product-gitlab-bootstrap:" + fingerprint).encode()).hexdigest()
    objects = ObjectStore(service.database.settings, service.database.security)
    async with service.database.sessions() as db:
        previous = await db.get(Effect, identity)
    if previous and (previous.fingerprint != fingerprint or previous.state not in {"preparing", "confirmed"}):
        raise ValueError("The GitLab preparation identity or receipt state changed.")
    state = json.loads(service.database.security.unseal(previous.resolution)) if previous and previous.resolution else {}
    if not previous:
        try:
            async with service.database.sessions.begin() as db:
                if await db.scalar(select(Effect.id).where(Effect.session_id == scope.session_id, Effect.state == "pending")):
                    raise ValueError("Resolve the pending workspace action before repository preparation.")
                db.add(Effect(
                    id=identity, session_id=scope.session_id, turn_id=scope.turn_id, actor_id=scope.actor_id,
                    tool_name="product_repository_prepare", arguments=service.database.security.seal(descriptor),
                    fingerprint=fingerprint, state="preparing",
                ))
        except IntegrityError:
            return await prepare_repository(service, scope, sandbox, context)
    if not state:
        await check()
        credentials = await service.credentials(scope, grant)
        token = validate_credentials(credentials, origin, target)

        async def inspect(bound):
            info = await bound.gitlab.request("GET", f"/projects/{target}", token=token, base_url=origin)
            name, _ = metadata(info, origin=origin, target=target, requested_url=url)
            branch = info.get("default_branch")
            if not valid_branch(branch):
                raise ValueError("GitLab did not confirm a usable default branch.")
            ref = context.get("commit_sha") or branch
            if context.get("commit_sha") and not SHA.fullmatch(str(ref)):
                raise ValueError("Use an exact GitLab commit SHA.")
            value = await bound.gitlab.request(
                "GET", f"/projects/{target}/repository/commits/{quote(ref, safe='')}", token=token, base_url=origin,
            )
            commit = value.get("id") if isinstance(value, dict) else None
            if not isinstance(commit, str) or not SHA.fullmatch(commit) or (context.get("commit_sha") and commit != ref):
                raise ValueError("GitLab did not confirm the requested commit.")
            return name, branch, commit

        name, branch, commit = await service.execution.call(inspect, authorize=check, recheck=True)
        state = {**descriptor, "repository": name, "branch": branch, "commit_sha": commit, "prepared": True}
        state["marker"] = service.database.security.seal({"fingerprint": fingerprint, "commit_sha": commit})
        async with service.database.sessions.begin() as db:
            row = await db.get(Effect, identity, with_for_update=True)
            assert row is not None
            if row.resolution:
                state = json.loads(service.database.security.unseal(row.resolution))
            else:
                row.resolution = service.database.security.seal(state)
    if (
        any(state.get(key) != value for key, value in descriptor.items())
        or not valid_branch(state.get("branch")) or not SHA.fullmatch(str(state.get("commit_sha", "")))
        or project_url(origin + "/" + state["repository"]).casefold() != url.casefold()
    ):
        raise ValueError("The saved GitLab preparation changed its frozen identity.")
    clone_url = origin + "/" + state["repository"] + ".git"
    if not state.get("bundle"):
        await check()
        async def transport_policy(bound):
            return bound.gitlab.allow_loopback_http
        allow_loopback_http = await service.execution.call(transport_policy)
        credentials = await service.credentials(scope, grant)
        token = validate_credentials(credentials, origin, target)
        with tempfile.TemporaryDirectory(prefix="moyai-gitlab-bundle-") as temporary:
            raw = await fetch_bundle(
                clone_url, state["branch"], state["commit_sha"], token, Path(temporary) / "source.bundle",
                allow_loopback_http=allow_loopback_http,
            )
        await check()
        state["bundle"], state["bundle_sha256"] = await objects.put(raw), hashlib.sha256(raw).hexdigest()
        async with service.database.sessions.begin() as db:
            row = await db.get(Effect, identity, with_for_update=True)
            assert row is not None and row.resolution is not None
            current = json.loads(service.database.security.unseal(row.resolution))
            if current.get("bundle"):
                state = current
            else:
                row.resolution = service.database.security.seal(state)
    raw = await objects.get(state["bundle"])
    await check()
    remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
    await sandbox.filesystem.write_bytes.aio(raw, remote)
    await check()
    receipt = {key: state[key] for key in (
        "repository_provider", "repository_origin", "gitlab_project_id", "repository_id",
        "repository", "branch", "commit_sha", "working_dir",
    )}
    process = await sandbox.exec.aio("python3", "-I", "-c", INSTALL, json.dumps({
        "bundle": remote, "bundle_sha256": state["bundle_sha256"], "marker": state["marker"],
        "branch": state["branch"], "commit": state["commit_sha"], "url": clone_url, "receipt": receipt,
    }), timeout=240)
    await process.wait.aio()
    if process.returncode:
        raise ValueError("GitLab preparation did not settle; existing user files were not replaced.")
    if json.loads(await process.stdout.read.aio()) != receipt:
        raise ValueError("The installed GitLab receipt did not match its reserved identity.")
    async with service.database.sessions.begin() as db:
        row = await db.get(Effect, identity, with_for_update=True)
        assert row is not None
        row.state, row.settled_at = "confirmed", now()
    await check()
    return receipt


async def repository_receipt(
    service: ProductConnections, db, *, session_id: str, workspace_id: str, workspace_scope: str, repo_url: str,
) -> dict | None:
    """Read exact historical evidence only; the caller must recheck authority."""
    url = project_url(repo_url)
    rows = list(await db.scalars(select(Effect).where(
        Effect.session_id == session_id, Effect.tool_name == "product_repository_prepare", Effect.state == "confirmed",
    )))
    for row in rows:
        if not row.resolution:
            continue
        value = json.loads(service.database.security.unseal(row.resolution))
        if (
            value.get("repository_provider") != "gitlab" or value.get("workspace_id") != workspace_id
            or value.get("workspace_scope") != workspace_scope or value.get("requested_url") != url
        ):
            continue
        target = value.get("gitlab_project_id")
        if (
            type(target) is not int or target <= 0 or value.get("repository_id") != target
            or value.get("repository_origin") != repository_origin(url)
            or value.get("access") != "selected_connection"
            or project_url(value["repository_origin"] + "/" + value.get("repository", "")).casefold() != url.casefold()
            or not SHA.fullmatch(str(value.get("commit_sha", ""))) or value.get("working_dir") != "/workspace/repo"
        ):
            raise PermissionError("The saved GitLab receipt has invalid project authority.")
        return {
            **{key: value[key] for key in (
                "repository_provider", "repository_origin", "gitlab_project_id", "repository_id",
                "commit_sha", "working_dir", "connection_id", "connection_revision",
            )},
            "repo_url": project_url(value["repository_origin"] + "/" + value["repository"]),
            "source": "native_repository_receipt", "public": False,
        }
    return None


async def repository_context(service: ProductConnections, scope: RunScope, context: dict[str, Any]) -> dict | None:
    await service.database.authorize_scope(scope)
    await service.require_scope(scope)
    async with service.database.sessions() as db:
        session = await db.get(Session, scope.session_id)
        if session is None or not session.workspace:
            return None
        workspace = json.loads(session.workspace)
        value = await repository_receipt(
            service, db, session_id=scope.session_id, workspace_id=workspace.get("id", ""),
            workspace_scope=scope.workspace_key, repo_url=str(context.get("repo_url") or ""),
        )
    if value is None:
        return None
    if (
        context.get("repository_provider", "gitlab") != "gitlab"
        or context.get("github_repository_id") is not None
        or context.get("repository_origin", value["repository_origin"]) != value["repository_origin"]
        or context.get("gitlab_project_id") not in {None, value["gitlab_project_id"]}
        or context.get("repository_id") not in {None, value["gitlab_project_id"]}
    ):
        raise PermissionError("The GitLab receipt changed the frozen repository selection.")
    selected = [
        grant for grant in scope.grants if grant.id == value["connection_id"]
        and grant.revision == value["connection_revision"]
        and grant_matches(grant, value["repository_origin"], value["gitlab_project_id"])
    ]
    if len(selected) != 1:
        raise PermissionError("The GitLab receipt's original project is no longer selected.")
    await service.check(scope, selected[0])
    await service.database.authorize_scope(scope)
    return value


async def resolve_repository(
    service: ProductConnections, grant, repository: str = "", repository_id: int | None = None,
    *, selected: bool, base_url: str | None = None,
) -> dict[str, Any]:
    """Resolve automation selection outside its writer transaction, privately."""
    if not selected:
        raise HTTPException(403, "GitLab bootstrap requires a selected project connection.")
    if repository_id is not None and (type(repository_id) is not int or repository_id <= 0):
        raise HTTPException(422, "Use the numeric GitLab project ID.")
    principal = await service.product.identity.require_current(grant)
    url = project_url(repository) if "://" in repository else ""
    before = await service.resolve_grants(principal.id, ["gitlab"], shared_session=True, surface="automation")
    if url:
        origin = repository_origin(url)
    elif base_url is not None:
        origin = canonical_origin(base_url)
    else:
        # Canonical grants project the saved credential's configured origin;
        # path-only enterprise events must never default to GitLab.com.
        origins = {
            canonical_origin(item.policy["product"]["base_url"])
            for item in before if item.kind == "gitlab" and (
                repository_id is None or item.policy.get("product", {}).get("repository_id") == repository_id
            )
        }
        if len(origins) != 1:
            raise HTTPException(403, "Choose the exact configured GitLab origin for this project.")
        origin = origins.pop()
    if base_url is not None and canonical_origin(base_url) != origin:
        raise HTTPException(422, "The GitLab URL does not match the configured origin.")
    path = urlsplit(url).path.lstrip("/") if url else repository
    if path:
        project_url(origin + "/" + path)
    candidates = [item for item in before if grant_matches(item, origin, repository_id)]
    if not candidates:
        raise HTTPException(403, "This project is not among the requester's selected GitLab grants.")
    original: Grant | None = None

    async def current_selection():
        await service.product.identity.require_current(grant)
        expected = [original] if original is not None else candidates
        current = await service.resolve_grants(
            principal.id, [item.id for item in expected], shared_session=True, surface="automation",
        )
        if any(item not in current for item in expected):
            raise HTTPException(409, "The selected GitLab authority changed during repository lookup.")

    async def resolve(bound):
        nonlocal original
        if bound.gitlab.normalize_base_url(origin) != origin:
            raise PermissionError("The configured GitLab origin changed.")
        credentials = bound.gitlab.saved_credentials()
        if canonical_origin(credentials.get("base_url", "")) != origin:
            raise PermissionError("The configured GitLab origin changed.")
        target = repository_id if repository_id is not None else bound.gitlab.target(path, credentials)
        matches = [item for item in candidates if grant_matches(item, origin, target)]
        if len(matches) != 1 or (path and bound.gitlab.target(path, credentials) != target):
            raise HTTPException(403, "The GitLab path and selected project ID do not match.")
        original = matches[0]
        token = validate_credentials(credentials, origin, target)
        if not bound.policy("gitlab")["enabled"]:
            raise HTTPException(403, "GitLab repository checkout is disabled by current policy.")
        info = await bound.gitlab.request("GET", f"/projects/{target}", token=token, base_url=origin)
        name, resolved_url = metadata(info, origin=origin, target=target, requested_url=origin + "/" + path if path else "")
        return {
            "repository_provider": "gitlab", "repository_origin": origin, "gitlab_project_id": target,
            "repository_id": target, "repository": name, "repo_url": resolved_url,
            "connection_id": original.id, "connection_revision": original.revision, "public": False,
        }

    await current_selection()
    result = await service.execution.call(resolve, authorize=current_selection, recheck=True)
    await current_selection()
    return result
