"""Scoped Git fetch on the trusted worker; only a credential-free bundle reaches Modal."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

from app.connector_errors import ConnectorError
from app.db import database as source_database
from app.github_write_access import GitHubWriteAccess
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .contracts import RunScope
from .models import Effect, Session, now
from .storage import ObjectStore

if TYPE_CHECKING:
    from .product_connections import ProductConnections

MAX_BUNDLE = 128 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

INSTALL = r"""
import ctypes, errno, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile
data = json.loads(sys.argv[1])
target = pathlib.Path("/workspace/repo")
bundle = pathlib.Path(data["bundle"])
if not re.fullmatch(r"/tmp/moyai-git-[0-9a-f]{32}\.bundle",str(bundle)):
    raise ValueError("Invalid repository bundle path.")
if target.parent.resolve() != target.parent:
    raise ValueError("The repository parent was redirected.")
def git(*args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        env={**os.environ, "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_CONFIG_SYSTEM":"/dev/null",
             "GIT_TERMINAL_PROMPT":"0", "GIT_LFS_SKIP_SMUDGE":"1"})
    return result.stdout.decode().strip()
def existing():
    if target.is_symlink() or not target.is_dir() or (target/".git").is_symlink():
        raise ValueError("An existing repository path cannot be replaced.")
    if (target/".git"/"moyai-bootstrap").read_text() != data["marker"]:
        raise ValueError("This checkout has different preparation provenance.")
    if git("-C",str(target),"config","--get","remote.origin.url") != data["url"]:
        raise ValueError("The checkout remote changed; it was not overwritten.")
    print(json.dumps(data["receipt"]))
if target.exists() or target.is_symlink():
    try: existing()
    finally: bundle.unlink(missing_ok=True)
else:
    if bundle.is_symlink():
        raise ValueError("Invalid repository bundle path.")
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != data["bundle_sha256"]:
        raise ValueError("Repository transfer integrity check failed.")
    stage = pathlib.Path(tempfile.mkdtemp(prefix=".moyai-checkout-",dir=str(target.parent)))
    try:
        checkout = stage/"repo"
        git("clone","--no-hardlinks","--no-local","--branch",data["branch"],str(bundle),str(checkout))
        if git("-C",str(checkout),"rev-parse","HEAD") != data["commit"]:
            raise ValueError("Repository base changed during transfer.")
        git("-C",str(checkout),"remote","set-url","origin",data["url"])
        (checkout/".git"/"moyai-bootstrap").write_text(data["marker"])
        libc = ctypes.CDLL(None,use_errno=True)
        if not hasattr(libc,"renameat2"):
            raise ValueError("Atomic no-replace checkout installation is unavailable.")
        libc.renameat2.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        if libc.renameat2(-100,os.fsencode(checkout),-100,os.fsencode(target),1):
            if ctypes.get_errno() != errno.EEXIST:
                raise OSError(ctypes.get_errno(),"Repository installation failed.")
        existing()
    finally:
        shutil.rmtree(stage)
        bundle.unlink(missing_ok=True)
"""


async def git(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "-c", "core.hooksPath=/dev/null", *command, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 180)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode or len(stdout) > 64_000:
        raise ValueError("The scoped Git fetch did not complete. No credential was sent to the computer.")
    return stdout.decode().strip()


async def fetch_bundle(url: str, branch: str, commit: str, token: str, destination: Path) -> bytes:
    environment = {key: os.environ[key] for key in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false", "GIT_CONFIG_COUNT": "3" if token else "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false",
    })
    if token:
        environment.update({
            "GIT_CONFIG_KEY_2": "http.extraHeader",
            "GIT_CONFIG_VALUE_2": "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode(),
        })
    with tempfile.TemporaryDirectory(prefix="moyai-git-source-") as temporary:
        root = Path(temporary)
        def directory_size() -> int:
            size = 0
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass  # Git atomically renames temporary pack files.
            return size
        await git(["init", "--bare", "."], root, environment)
        task = asyncio.create_task(git(["fetch", "--no-tags", url, commit], root, environment))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=.5)
                size = await asyncio.to_thread(directory_size)
                if size > MAX_BUNDLE:
                    raise ValueError("Repository history exceeds the 128 MiB bootstrap budget; select a validated environment.")
            await task
            await git(["update-ref", "refs/heads/" + branch, commit], root, environment)
            await git(["bundle", "create", str(destination), "--branches"], root, environment)
            if destination.stat().st_size > MAX_BUNDLE:
                raise ValueError("Repository bundle exceeds the bootstrap budget.")
            return await asyncio.to_thread(destination.read_bytes)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def prepare_repository(
    service: ProductConnections, scope: RunScope, sandbox: Any, context: dict[str, Any],
) -> dict[str, Any]:
    if not context.get("repo_url") or context.get("environment_snapshot_id"):
        return {"prepared": False, "reason": "No bare repository checkout requested."}
    if context.get("working_dir") != "/workspace/repo":
        raise ValueError("Use the canonical repository working directory.")
    await service.database.authorize_scope(scope)
    await service.require_scope(scope)
    url = str(context["repo_url"])
    repository = url.removeprefix("https://github.com/").removesuffix("/").removesuffix(".git")
    if not url.startswith("https://github.com/") or not REPOSITORY.fullmatch(repository):
        raise ValueError("Use an exact credential-free GitHub repository URL.")
    explicit_target = context.get("repository_id")
    target = explicit_target
    if target is None and any(grant.kind == "github" for grant in scope.grants):
        try:
            target = await source_database(service.connectors.github.target, repository)
        except ConnectorError:
            target = None
    selected = [grant for grant in scope.grants if (
        grant.kind == "github" and (grant.policy.get("product") or {}).get("repository_id") == target
    )]
    grant = selected[0] if len(selected) == 1 else None
    public_info = None
    if grant is None:
        target = explicit_target
        public_info = await service.public_repository(repository, target, refresh=True)
        if (
            not isinstance(public_info, dict) or public_info.get("private") is not False
            or type(public_info.get("id")) is not int or public_info["id"] <= 0
        ):
            raise HTTPException(403, "Private repository checkout requires its exact selected GitHub connection.")
        target = public_info["id"]
    else:
        permitted = await source_database(service.connectors.allowed, "github_checkout")
        if not permitted:
            raise HTTPException(403, "Repository checkout is disabled by the current connection policy.")
    async def check():
        if grant is None:
            await service.database.authorize_scope(scope)
            await service.require_scope(scope)
        else:
            await service.check(scope, grant)
    await check()
    descriptor = {
        "session_id": scope.session_id, "workspace_id": sandbox.object_id, "workspace_scope": scope.workspace_key,
        "repository_id": target, "connection_id": grant.id if grant else "", "connection_revision": grant.revision if grant else 0,
        "working_dir": "/workspace/repo", "requested_url": url.rstrip("/"), "access": "selected_connection" if grant else "public",
    }
    fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    identity = hashlib.sha256(("product-github-bootstrap:" + fingerprint).encode()).hexdigest()
    objects = ObjectStore(service.database.settings, service.database.security)
    async with service.database.sessions() as db:
        previous = await db.get(Effect, identity)
    if previous and previous.fingerprint != fingerprint:
        raise ValueError("The repository preparation identity changed.")
    state = json.loads(service.database.security.unseal(previous.resolution)) if previous and previous.resolution else {}
    if previous and previous.state not in {"preparing", "confirmed"}:
        raise ValueError("This repository preparation has an unsupported receipt state.")
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
        credential = await service.credentials(scope, grant) if grant else {"access_token": ""}
        async def inspect(bound):
            info = public_info or await bound.github.request("GET", f"/repositories/{target}", token=credential["access_token"])
            branch = info.get("default_branch", "")
            name = info.get("full_name", "")
            if info.get("id") != target or not REPOSITORY.fullmatch(name) or not GitHubWriteAccess.valid_branch(branch):
                raise ValueError("GitHub did not confirm the selected repository identity.")
            commit = context.get("commit_sha")
            if not commit:
                value = await bound.github.request("GET", f"/repositories/{target}/commits/{quote(branch, safe='')}", token=credential["access_token"])
                commit = value.get("sha", "")
            if not isinstance(commit, str) or not SHA.fullmatch(commit):
                raise ValueError("GitHub did not confirm a valid repository commit.")
            return name, branch, commit

        name, branch, commit = await service.source_call(inspect)
        state = {**descriptor, "repository": name, "branch": branch, "commit_sha": commit, "prepared": True}
        state["marker"] = service.database.security.seal({"fingerprint": fingerprint, "commit_sha": commit})
        async with service.database.sessions.begin() as db:
            row = await db.get(Effect, identity, with_for_update=True)
            assert row is not None
            if row.resolution:
                state = json.loads(service.database.security.unseal(row.resolution))
            else:
                row.resolution = service.database.security.seal(state)
    url = "https://github.com/" + state["repository"] + ".git"
    if not state.get("bundle"):
        credential = await service.credentials(scope, grant) if grant else {"access_token": ""}
        with tempfile.TemporaryDirectory(prefix="moyai-git-bundle-") as temporary:
            raw = await fetch_bundle(url, state["branch"], state["commit_sha"], credential["access_token"], Path(temporary) / "source.bundle")
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
    receipt = {key: state[key] for key in ("repository_id", "repository", "branch", "commit_sha", "working_dir")}
    process = await sandbox.exec.aio("python3", "-I", "-c", INSTALL, json.dumps({
        "bundle": remote, "bundle_sha256": state["bundle_sha256"], "marker": state["marker"],
        "branch": state["branch"], "commit": state["commit_sha"], "url": url, "receipt": receipt,
    }), timeout=240)
    await process.wait.aio()
    if process.returncode:
        raise ValueError("Repository preparation did not settle; existing user files were not replaced.")
    result = json.loads(await process.stdout.read.aio())
    if result != receipt:
        raise ValueError("The installed repository receipt did not match its reserved identity.")
    async with service.database.sessions.begin() as db:
        row = await db.get(Effect, identity, with_for_update=True)
        assert row is not None
        row.state, row.settled_at = "confirmed", now()
    await check()
    return receipt


async def repository_receipt(
    service: ProductConnections, db, *, session_id: str, workspace_id: str, workspace_scope: str, repo_url: str,
) -> dict | None:
    """Read evidence in the caller's authorized transaction; this grants no access.

    Historical callers must authenticate the original turn and pass its exact
    persisted workspace reference, then recheck current repository access.
    """
    rows = list(await db.scalars(select(Effect).where(
        Effect.session_id == session_id, Effect.tool_name == "product_repository_prepare", Effect.state == "confirmed",
    )))
    for row in rows:
        if not row.resolution:
            continue
        value = json.loads(service.database.security.unseal(row.resolution))
        if (
            value.get("workspace_id") != workspace_id or value.get("workspace_scope") != workspace_scope
            or value.get("requested_url") != repo_url.rstrip("/")
        ):
            continue
        return {"repository_id": value["repository_id"], "repo_url": "https://github.com/" + value["repository"],
                "commit_sha": value["commit_sha"], "working_dir": value["working_dir"],
                "source": "native_repository_receipt", "public": value.get("access") == "public",
                "connection_id": value.get("connection_id"), "connection_revision": value.get("connection_revision")}
    return None


async def repository_context(service: ProductConnections, scope: RunScope, context: dict[str, Any]) -> dict | None:
    """Project a live execution's verified ID without rewriting its descriptor."""
    await service.database.authorize_scope(scope)
    await service.require_scope(scope)
    async with service.database.sessions() as db:
        session = await db.get(Session, scope.session_id)
        if session is None or not session.workspace:
            return None
        workspace = json.loads(session.workspace)
        value = await repository_receipt(
            service, db, session_id=scope.session_id, workspace_id=workspace.get("id", ""),
            workspace_scope=scope.workspace_key, repo_url=str(context.get("repo_url", "")),
        )
    if value is None:
        return None
    if value["public"]:
        info = await service.public_repository(repository_id=value["repository_id"])
        if not isinstance(info, dict) or info.get("id") != value["repository_id"] or info.get("private") is not False:
            return None
    else:
        selected = next((grant for grant in scope.grants if grant.id == value["connection_id"]), None)
        if selected is None:
            return None
        await service.check(scope, selected)
    await service.database.authorize_scope(scope)
    return value
