"""Supplementary identity/receipt tests; actual-app proof belongs to deployment."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.connector_errors import ConnectorError
from app.gitlab_transport import normalize_base_url
from fastapi import HTTPException
from sqlalchemy import select

from moyai import product_gitlab_bootstrap as bootstrap
from moyai.contracts import Grant, RunScope
from moyai.models import Effect


def project_grant(*, provider="gitlab", origin="https://gitlab.example.test", target=42, revision=1):
    return Grant(
        f"{provider}-{origin}-{target}", provider, revision, "",
        policy={"product": {"repository_id": target, "base_url": origin}},
    )


def context(**changes):
    return {
        "repo_url": "https://gitlab.example.test/group/subgroup/project", "repository_provider": "gitlab",
        "repository_origin": "https://gitlab.example.test", "gitlab_project_id": 42,
        "repository_id": 42, "working_dir": "/workspace/repo", **changes,
    }


class Execution:
    def __init__(self, bound):
        self.bound = bound

    async def call(self, operation, *, authorize=None, recheck=False):
        if authorize:
            await authorize()
        result = await operation(self.bound)
        if authorize and recheck:
            await authorize()
        return result


@pytest.fixture
def service():
    info = {
        "id": 42, "path_with_namespace": "group/subgroup/project", "default_branch": "main",
        "web_url": "https://gitlab.example.test/group/subgroup/project",
    }

    async def request(method, path, *, token, base_url):
        assert method == "GET" and token == "synthetic-bootstrap-token"
        assert base_url == "https://gitlab.example.test"
        return info.copy() if path == "/projects/42" else {"id": "a" * 40}

    credentials = {
        "access_token": "synthetic-bootstrap-token", "base_url": "https://gitlab.example.test",
        "repository_ids": [42], "repositories": [{"id": 42, "full_name": "group/subgroup/project"}],
    }
    helper = SimpleNamespace(
        request=AsyncMock(side_effect=request), target=Mock(return_value=42),
        saved_credentials=Mock(return_value=credentials),
        normalize_base_url=normalize_base_url, allow_loopback_http=False,
    )
    bound = SimpleNamespace(gitlab=helper, policy=Mock(return_value={"enabled": True}))
    return SimpleNamespace(
        database=SimpleNamespace(authorize_scope=AsyncMock()), require_scope=AsyncMock(), check=AsyncMock(),
        credentials=AsyncMock(return_value=credentials), execution=Execution(bound), info=info,
        product=SimpleNamespace(identity=SimpleNamespace(
            require_current=AsyncMock(return_value=SimpleNamespace(id="actor")),
        )), resolve_grants=AsyncMock(return_value=[project_grant()]),
    )


@pytest.mark.parametrize("grants", [
    [], [project_grant(provider="github")], [project_grant(target=43)],
    [project_grant(origin="https://other.example.test")],
])
async def test_unselected_identity_fails_before_credential_read(service, grants):
    scope = RunScope("session", "turn", "actor", "model", grants)
    with pytest.raises(HTTPException, match="403"):
        await bootstrap.prepare_repository(service, scope, SimpleNamespace(object_id="sandbox"), context())
    service.credentials.assert_not_awaited()
    service.execution.bound.gitlab.request.assert_not_awaited()
    service.execution.bound.gitlab.saved_credentials.assert_not_called()


@pytest.mark.parametrize("change", [
    {"repository_provider": "github"}, {"github_repository_id": 42},
    {"repository_origin": "https://other.example.test"}, {"repository_id": 43},
    {"gitlab_project_id": True}, {"repo_url": "https://user:secret@gitlab.example.test/group/project"},
])
async def test_frozen_identity_conflict_is_rejected_before_credentials(service, change):
    scope = RunScope("session", "turn", "actor", "model", [project_grant()])
    with pytest.raises(ValueError):
        await bootstrap.prepare_repository(service, scope, SimpleNamespace(object_id="sandbox"), context(**change))
    service.credentials.assert_not_awaited()


async def test_revoked_selected_grant_never_falls_back_to_public(service):
    service.check.side_effect = PermissionError("revoked")
    scope = RunScope("session", "turn", "actor", "model", [project_grant()])
    with pytest.raises(PermissionError, match="revoked"):
        await bootstrap.prepare_repository(service, scope, SimpleNamespace(object_id="sandbox"), context())
    service.credentials.assert_not_awaited()


@pytest.mark.parametrize("change", [
    {"base_url": "https://other.example.test"}, {"repository_ids": [43]}, {"access_token": ""},
])
def test_credential_ceiling_cannot_widen(change):
    with pytest.raises(PermissionError):
        bootstrap.validate_credentials({
            "base_url": "https://gitlab.example.test", "repository_ids": [42], "access_token": "synthetic", **change,
        }, "https://gitlab.example.test", 42)


@pytest.mark.parametrize("change", [
    {"id": 43}, {"id": True}, {"web_url": "https://other.example.test/group/subgroup/project"},
    {"path_with_namespace": "other/project"}, {"web_url": "https://user@gitlab.example.test/group/subgroup/project"},
])
def test_metadata_must_verify_origin_path_and_project_id(service, change):
    with pytest.raises(ValueError):
        bootstrap.metadata(
            {**service.info, **change}, origin="https://gitlab.example.test",
            target=42, requested_url=context()["repo_url"],
        )


async def test_preflight_uses_exact_selected_origin_and_returns_provider_identity(service):
    result = await bootstrap.resolve_repository(
        service, "principal", context()["repo_url"], 42, selected=True,
        base_url="https://gitlab.example.test",
    )
    assert result["gitlab_project_id"] == result["repository_id"] == 42
    assert result["repository_provider"] == "gitlab"
    assert result["repository_origin"] == "https://gitlab.example.test"
    assert result["repo_url"] == context()["repo_url"]
    assert result["public"] is False


@pytest.mark.parametrize("kwargs", [
    {"selected": False}, {"selected": True, "base_url": "https://other.example.test"},
])
async def test_preflight_does_not_use_private_credentials_for_public_or_other_origin(service, kwargs):
    with pytest.raises(HTTPException):
        await bootstrap.resolve_repository(service, "principal", context()["repo_url"], 42, **kwargs)
    service.execution.bound.gitlab.saved_credentials.assert_not_called()
    service.execution.bound.gitlab.request.assert_not_awaited()


@pytest.fixture
def sandbox():
    transferred = []
    calls = []

    async def write(raw, destination):
        transferred.append((raw, destination))

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        payload = json.loads(args[-1])
        assert "synthetic-bootstrap-token" not in json.dumps(payload)
        assert payload["url"] == context()["repo_url"] + ".git"
        return SimpleNamespace(
            returncode=0, wait=SimpleNamespace(aio=AsyncMock()),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=json.dumps(payload["receipt"])))),
        )

    return SimpleNamespace(
        object_id="sandbox", filesystem=SimpleNamespace(write_bytes=SimpleNamespace(aio=AsyncMock(side_effect=write))),
        exec=SimpleNamespace(aio=AsyncMock(side_effect=execute)), transferred=transferred, calls=calls,
    )


async def test_bootstrap_receipt_replay_freezes_commit_without_reusing_token_in_guest(
    service, sandbox, active, runtime, monkeypatch,
):
    _, _, prepared = active
    service.database = SimpleNamespace(
        authorize_scope=AsyncMock(), sessions=runtime.database.sessions,
        settings=runtime.settings, security=runtime.security,
    )
    scope = replace(prepared.scope, grants=[project_grant()])
    fetched = AsyncMock(return_value=b"credential-free-git-bundle")
    monkeypatch.setattr(bootstrap, "fetch_bundle", fetched)
    receipt = await bootstrap.prepare_repository(service, scope, sandbox, context())
    assert receipt["commit_sha"] == "a" * 40
    assert receipt["gitlab_project_id"] == 42 and receipt["repository_provider"] == "gitlab"
    assert sandbox.transferred[0][0] == b"credential-free-git-bundle"
    assert fetched.await_args.args[:4] == (
        context()["repo_url"] + ".git", "main", "a" * 40, "synthetic-bootstrap-token",
    )
    service.execution.bound.gitlab.request.reset_mock()
    service.credentials.reset_mock()
    assert await bootstrap.prepare_repository(service, scope, sandbox, context()) == receipt
    fetched.assert_awaited_once()
    service.credentials.assert_not_awaited()
    service.execution.bound.gitlab.request.assert_not_awaited()
    async with runtime.database.sessions() as db:
        row = await db.scalar(select(Effect).where(Effect.tool_name == "product_repository_prepare"))
        assert row.state == "confirmed"
        saved = runtime.security.unseal(row.resolution)
        assert b"synthetic-bootstrap-token" not in saved
        result = await bootstrap.repository_receipt(
            service, db, session_id=scope.session_id, workspace_id="sandbox",
            workspace_scope=scope.workspace_key, repo_url=context()["repo_url"],
        )
        assert result["repository_origin"] == "https://gitlab.example.test"
        assert result["gitlab_project_id"] == 42 and result["public"] is False
        assert await bootstrap.repository_receipt(
            service, db, session_id=scope.session_id, workspace_id="later-sandbox",
            workspace_scope=scope.workspace_key, repo_url=context()["repo_url"],
        ) is None
    service.check.side_effect = PermissionError("revoked")
    with pytest.raises(PermissionError, match="revoked"):
        await bootstrap.prepare_repository(service, scope, sandbox, context())
    assert len(sandbox.calls) == 2


async def test_revocation_after_fetch_prevents_transfer(service, sandbox, active, runtime, monkeypatch):
    _, _, prepared = active
    service.database = SimpleNamespace(
        authorize_scope=AsyncMock(), sessions=runtime.database.sessions,
        settings=runtime.settings, security=runtime.security,
    )
    scope = replace(prepared.scope, grants=[project_grant()])

    async def fetch(*args, **kwargs):
        service.check.side_effect = PermissionError("revoked after fetch")
        return b"bundle"

    monkeypatch.setattr(bootstrap, "fetch_bundle", fetch)
    with pytest.raises(PermissionError, match="revoked after fetch"):
        await bootstrap.prepare_repository(service, scope, sandbox, context())
    sandbox.filesystem.write_bytes.aio.assert_not_awaited()


async def test_actual_git_bundle_has_no_auth_and_fetches_exact_commit_over_loopback(tmp_path):
    """Real Git objects/HTTP, not a claim of full application E2E."""
    source = tmp_path / "source"
    source.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=source, env=env, stderr=subprocess.DEVNULL,
        ).decode().strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "local@example.test")
    (source / "file.txt").write_text("Exact first revision\n")
    git("add", "file.txt")
    git("commit", "-m", "First")
    commit = git("rev-parse", "HEAD")
    (source / "file.txt").write_text("Later revision\n")
    git("commit", "-am", "Second")
    root = tmp_path / "http"
    bare = root / "group" / "subgroup" / "project.git"
    bare.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(bare))
    git("--git-dir", str(bare), "update-server-info")
    token = "synthetic-token-never-export"
    expected = "Basic " + base64.b64encode(("oauth2:" + token).encode()).decode()
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.headers.get("Authorization") != expected:
                self.send_error(401)
                return
            return super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = tmp_path / "transfer.bundle"
        raw = await asyncio.wait_for(bootstrap.fetch_bundle(
            f"http://127.0.0.1:{server.server_port}/group/subgroup/project.git",
            "main", commit, token, output, allow_loopback_http=True,
        ), 20)
        assert token.encode() not in raw and expected.encode() not in raw
        copied = tmp_path / "copied"
        git("clone", "--branch", "main", str(output), str(copied))
        assert git("-C", str(copied), "rev-parse", "HEAD") == commit
        assert (copied / "file.txt").read_text() == "Exact first revision\n"
        assert token not in (copied / ".git" / "config").read_text()
        assert requests
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def test_path_only_enterprise_preflight_uses_selected_configured_origin(service):
    result = await bootstrap.resolve_repository(
        service, "principal", "group/subgroup/project", 42, selected=True,
    )
    assert result["repository_origin"] == "https://gitlab.example.test"
    assert service.execution.bound.gitlab.request.await_args.kwargs["base_url"] == "https://gitlab.example.test"


async def test_git_transport_rejects_loopback_without_explicit_opt_in():
    with pytest.raises(ConnectorError):
        await bootstrap.git_transport_environment("http://127.0.0.1:8929/group/project", "synthetic")


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "::1"],
    ["::ffff:127.0.0.1"], ["2002:0808:0808::1"], [],
])
async def test_git_transport_rejects_private_mixed_or_tunneled_dns(monkeypatch, addresses):
    resolver = AsyncMock(return_value=[
        (2, 1, 6, "", (address, 443)) for address in addresses
    ])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    with pytest.raises(ValueError, match="permitted"):
        await bootstrap.git_transport_environment("https://gitlab.example.test/group/project", "synthetic")


async def test_git_transport_pins_validated_dns_and_cannot_follow_redirects_or_inherit_proxy(monkeypatch):
    resolver = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.example.test")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "http.followRedirects=true")
    env = await bootstrap.git_transport_environment("https://gitlab.example.test/group/project", "synthetic")
    config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert config["http.curloptResolve"] == "gitlab.example.test:443:8.8.8.8"
    assert config["http.followRedirects"] == "false" and config["http.proxy"] == ""
    assert "http.https://gitlab.example.test/.extraHeader" in config
    assert "HTTPS_PROXY" not in env and "GIT_CONFIG_PARAMETERS" not in env


async def test_old_git_cannot_silently_ignore_dns_pinning(monkeypatch):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(
        return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
    ))
    probe = AsyncMock(return_value="http.followRedirects\nhttp.proxy")
    monkeypatch.setattr(bootstrap, "git", probe)
    with pytest.raises(ValueError, match="support pinned"):
        await bootstrap.git_transport_environment("https://gitlab.example.test/group/project", "synthetic-secret")
    assert "synthetic-secret" not in repr(probe.await_args)
