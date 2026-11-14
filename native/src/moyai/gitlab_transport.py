"""Credential-confined trusted-worker Git, with exact branch compare-and-swap."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.github import Change
from pydantic_ai.exceptions import ToolFailed

from .gitlab_workflow_types import PROJECT_PATH, branch_name, origin

MAX_BUNDLE = 128 * 1024 * 1024
OID = re.compile(r"^[0-9a-f]{40}$")


def remote_url(base_url: str, project_path: str) -> str:
    base = origin(base_url)
    if not re.fullmatch(PROJECT_PATH, project_path) or any(
        part in {".", ".."} or part.endswith(".git") for part in project_path.split("/")
    ):
        raise ValueError("Invalid GitLab project path.")
    return base + "/" + project_path + ".git"


def permitted_remote(base_url: str, project_path: str, allow_loopback_http: bool) -> str:
    url = remote_url(base_url, project_path)
    if url.startswith("http:") and not allow_loopback_http:
        raise ValueError("Loopback Git transport requires explicit local-test configuration.")
    return url


def environment(token: str) -> dict[str, str]:
    # No inherited Git configuration, credential helpers, tracing or proxy env.
    env = {key: os.environ[key] for key in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    options = [
        ("credential.helper", ""), ("http.followRedirects", "false"),
        ("protocol.allow", "never"), ("protocol.https.allow", "always"),
        ("protocol.http.allow", "always"), ("http.sslVerify", "true"),
        ("http.proxy", ""), ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"), ("maintenance.auto", "false"),
    ]
    if token:
        options.append(("http.extraHeader", "Authorization: Basic " + base64.b64encode(
            ("oauth2:" + token).encode(),
        ).decode()))
    env.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_COUNT": str(len(options)),
        "GIT_AUTHOR_NAME": "Moyai", "GIT_AUTHOR_EMAIL": "moyai@localhost",
        "GIT_COMMITTER_NAME": "Moyai", "GIT_COMMITTER_EMAIL": "moyai@localhost",
    })
    for index, (key, value) in enumerate(options):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


async def trusted_environment(url: str, token: str, *, allow_loopback_http: bool) -> dict[str, str]:
    from .product_gitlab_bootstrap import git_transport_environment

    env = await git_transport_environment(url, token, allow_loopback_http=allow_loopback_http)
    options = [
        ("protocol.allow", "never"), ("protocol.https.allow", "always"), ("protocol.http.allow", "always"),
        ("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"), ("maintenance.auto", "false"),
        ("http.sslVerify", "true"),
    ]
    offset = int(env["GIT_CONFIG_COUNT"])
    for index, (key, value) in enumerate(options, start=offset):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(offset + len(options))
    env.update({
        "GIT_AUTHOR_NAME": "Moyai", "GIT_AUTHOR_EMAIL": "moyai@localhost",
        "GIT_COMMITTER_NAME": "Moyai", "GIT_COMMITTER_EMAIL": "moyai@localhost",
    })
    return env


async def git(
    args: list[str], root: Path, env: dict[str, str], *, data: bytes | None = None,
    limit: int = MAX_BUNDLE,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=root, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), 180)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode or len(stdout) > limit:
        # Never include command output or env in errors: refs and hooks can echo secrets.
        raise ToolFailed("The trusted Git operation was not acknowledged; inspect publication_status before retrying.")
    return stdout


async def fetch(root: Path, env: dict[str, str], url: str, commit: str) -> None:
    if not OID.fullmatch(commit) or commit == "0" * 40:
        raise ValueError("An exact nonzero commit is required.")
    await git(["init", "--bare", "."], root, env)
    task = asyncio.create_task(git([
        "fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
        "--no-write-fetch-head", url, commit,
    ], root, env))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.5)
            def size() -> int:
                total = 0
                for path in root.rglob("*"):
                    try:
                        if path.is_file():
                            total += path.stat().st_size
                    except FileNotFoundError:
                        pass
                return total
            if await asyncio.to_thread(size) > MAX_BUNDLE:
                raise ToolFailed("GitLab history exceeds the 128 MiB trusted transport budget.")
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    actual = (await git(["rev-parse", "--verify", commit + "^{commit}"], root, env)).decode().strip()
    if actual != commit:
        raise ToolFailed("GitLab did not return the exact requested commit.")


async def fetch_bundle(
    *, base_url: str, project_path: str, commit: str, branch: str, token: str, destination: Path, allow_loopback_http: bool = False,
) -> bytes:
    """Public bootstrap interface. Caller guards current scope around this read."""
    branch_name(branch)
    url = permitted_remote(base_url, project_path, allow_loopback_http)
    env = await trusted_environment(url, token, allow_loopback_http=allow_loopback_http)
    with tempfile.TemporaryDirectory(prefix="moyai-gitlab-fetch-") as temporary:
        root = Path(temporary)
        await fetch(root, env, url, commit)
        await git(["update-ref", "refs/heads/" + branch, commit], root, env)
        await git(["bundle", "create", str(destination), "--branches"], root, env)
        if destination.stat().st_size > MAX_BUNDLE:
            raise ToolFailed("GitLab bundle exceeds 128 MiB.")
        return await asyncio.to_thread(destination.read_bytes)


@dataclass(frozen=True)
class PreparedCommit:
    sha: str
    bundle: bytes


async def prepare_commit(
    *, base_url: str, project_path: str, base_sha: str, token: str,
    files: list[Change], title: str, timestamp: int, allow_loopback_http: bool = False,
) -> PreparedCommit:
    url = permitted_remote(base_url, project_path, allow_loopback_http)
    env = await trusted_environment(url, token, allow_loopback_http=allow_loopback_http)
    env.update(GIT_AUTHOR_DATE=f"{timestamp} +0000", GIT_COMMITTER_DATE=f"{timestamp} +0000")
    with tempfile.TemporaryDirectory(prefix="moyai-gitlab-commit-") as temporary:
        root = Path(temporary)
        await fetch(root, env, url, base_sha)
        tree = await git(["ls-tree", "-rz", "--full-tree", base_sha], root, env, limit=20 * 1024 * 1024)
        entries: dict[str, str] = {}
        for raw in tree.split(b"\0"):
            if raw:
                header, name = raw.split(b"\t", 1)
                entries[name.decode("utf-8")] = header.decode().split()[0]
        folded = {name.casefold(): name for name in entries}
        await git(["read-tree", base_sha], root, env)
        for change in files:
            parts = change.path.split("/")
            if change.path.casefold() in folded and folded[change.path.casefold()] != change.path:
                raise ToolFailed("A publication path collides with an existing path's letter case.")
            for index in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:index])
                if ancestor in entries and (index < len(parts) or entries[ancestor] not in {"100644", "100755"}):
                    raise ToolFailed("Publication cannot traverse symlinks/submodules or replace a non-file path.")
            if any(name.startswith(change.path + "/") for name in entries):
                raise ToolFailed("Publication cannot replace a directory with a file.")
            if change.content is None:
                if change.path not in entries:
                    raise ToolFailed("The deletion path does not exist in the exact base tree.")
                record = f"0 {'0' * 40}\t{change.path}\0".encode()
            else:
                content = base64.b64decode(change.content, validate=True) if change.encoding == "base64" else change.content.encode()
                oid = (await git(["hash-object", "-w", "--stdin"], root, env, data=content)).decode().strip()
                mode = "100755" if change.executable else "100644"
                record = f"{mode} {oid}\t{change.path}\0".encode()
            await git(["update-index", "-z", "--index-info"], root, env, data=record)
        tree_sha = (await git(["write-tree"], root, env)).decode().strip()
        sha = (await git(["commit-tree", tree_sha, "-p", base_sha], root, env, data=title.encode())).decode().strip()
        if not OID.fullmatch(sha):
            raise ToolFailed("The prepared Git commit identity is invalid.")
        await git(["update-ref", "refs/heads/moyai-prepared", sha], root, env)
        destination = root / "prepared.bundle"
        await git(["bundle", "create", str(destination), "refs/heads/moyai-prepared"], root, env)
        if destination.stat().st_size > MAX_BUNDLE:
            raise ToolFailed("Prepared publication exceeds the trusted Git budget.")
        return PreparedCommit(sha, destination.read_bytes())


async def push(
    *, base_url: str, project_path: str, token: str, branch: str, commit: str,
    expected_head: str | None, bundle: bytes | None = None, allow_rebase: bool = False,
    allow_loopback_http: bool = False,
) -> dict:
    """Only this trusted primitive can update refs; an empty lease means absent."""
    branch_name(branch)
    if not OID.fullmatch(commit) or commit == "0" * 40 or (
        expected_head is not None and (not OID.fullmatch(expected_head) or expected_head == "0" * 40)
    ):
        raise ValueError("Invalid exact Git ref identity.")
    url = permitted_remote(base_url, project_path, allow_loopback_http)
    env = await trusted_environment(url, token, allow_loopback_http=allow_loopback_http)
    with tempfile.TemporaryDirectory(prefix="moyai-gitlab-push-") as temporary:
        root = Path(temporary)
        if bundle is None:
            await fetch(root, env, url, commit)
        else:
            if len(bundle) > MAX_BUNDLE:
                raise ToolFailed("Prepared bundle exceeds the transport budget.")
            await git(["init", "--bare", "."], root, env)
            source = root / "source.bundle"
            source.write_bytes(bundle)
            # A local bundle is data, never an authenticated arbitrary transport.
            await git(["-c", "protocol.file.allow=always", "fetch", "--no-tags",
                       "--no-write-fetch-head", str(source), commit], root, env)
        if expected_head and not allow_rebase:
            # The lease guards concurrency; ancestry independently guards rewrite intent.
            await git(["merge-base", "--is-ancestor", expected_head, commit], root, env)
        ref = "refs/heads/" + branch
        output = (await git([
            "push", "--porcelain", "--no-verify", "--no-follow-tags",
            "--force-with-lease=" + ref + ":" + (expected_head or ""),
            url, commit + ":" + ref,
        ], root, env, limit=64_000)).decode()
        records = [line.split("\t") for line in output.splitlines() if "\t" in line]
        # "up to date" is NOT proof this operation performed the write.
        matches = [parts for parts in records if len(parts) >= 3 and
                   parts[1] == commit + ":" + ref and parts[0] in {"*", " ", "+"}]
        if len(matches) != 1:
            raise ToolFailed("Git did not acknowledge the exact requested ref transition.")
        return {"provider": "gitlab", "origin": origin(base_url), "project": project_path,
                "branch": branch, "before": expected_head, "commit": commit, "acknowledged": True}
