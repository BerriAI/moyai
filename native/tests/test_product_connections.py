"""Canonical local stores and native toolsets; no real OAuth/provider calls."""

import asyncio
import json
import threading
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from app.connector_errors import ConnectorError
from app.db import Store
from app.security import Security
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from test_product_identity import settings_for

from moyai.contracts import RunScope
from moyai.product_connections import ProductConnections, ProductGitHubToolset


@pytest.fixture
def bridge(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.data_dir, settings.agent_model)
    security = Security(settings)
    with store.connect() as db:
        db.execute(
            "INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("google:owner", "google", "owner@example.test", "Owner", "2026-10-15", "2026-10-15"),
        )

    async def authorize(scope):
        assert scope.actor_id == "google:owner"
        return await service.resolve_grants(
            scope.actor_id, [g.id for g in scope.grants],
            shared_session=scope.authority.visibility == "shared", surface=scope.authority.surface,
        )

    service = ProductConnections(
        SimpleNamespace(store_proxy=store, security=security, settings=settings),
        SimpleNamespace(authorize_scope=authorize),
    )
    yield service
    store.close()


def save(service, provider, **extra):
    service.connectors.save(provider, {"access_token": "synthetic-token", "kind": "personal", **extra}, provider)


@pytest.mark.parametrize('bot', [False, True])
@pytest.mark.parametrize('identity_linking', [False, True])
@pytest.mark.parametrize('thread_chat', [False, True])
def test_slack_oauth_retains_existing_scope_contract(bridge, bot, identity_linking, thread_chat):
    from urllib.parse import parse_qs, urlsplit

    bridge.settings.slack_bot_enabled = bot
    bridge.settings.slack_identity_linking_enabled = identity_linking
    bridge.settings.slack_thread_chat_enabled = thread_chat
    params = parse_qs(urlsplit(bridge.connectors.authorization_url('slack', 'local-state')).query)
    assert params['user_scope'] == ['search:read,channels:history,groups:history,im:history,mpim:history']
    if not bot:
        assert 'scope' not in params
        return
    expected = {
        'app_mentions:read', 'chat:write', 'chat:write.customize', 'im:write', 'im:history', 'reactions:read',
        'reactions:write', 'assistant:write', 'files:write', 'files:read',
    }
    if identity_linking:
        expected |= {'users:read', 'users:read.email'}
    if thread_chat:
        expected |= {'channels:history', 'groups:history', 'channels:read', 'groups:read'}
    assert set(params['scope'][0].split(',')) == expected


async def scope_for(bridge, kind):
    grants = await bridge.resolve_grants("google:owner", [kind])
    return RunScope("native-session", "native-turn", "google:owner", "test", grants)


@pytest.mark.parametrize("provider", ["github", "gitlab", "linear", "notion", "slack"])
@pytest.mark.parametrize("read_only", [False, True])
async def test_canonical_definitions_do_not_depend_on_turn_identity(bridge, monkeypatch, provider, read_only):
    if provider == "github":
        credentials = github_accounts(bridge, 1)
        bridge.connectors.github.remember_repository(
            {"id": 1, "full_name": "org/repository", "owner": {"id": 1}}, credentials,
        )

        async def native(self, *, write=False):
            tools = FunctionToolset(id="github-provider")
            tools.add_function(
                lambda owner, repo: None, name="get_repository",
                metadata={"annotations": {"readOnlyHint": True}},
            )
            return tools

        monkeypatch.setattr(ProductGitHubToolset, "_native", native)
    elif provider == "gitlab":
        gitlab_accounts(bridge)
    else:
        save(bridge, provider)
    bridge.store.execute(
        "INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,1,?) "
        "ON CONFLICT(provider) DO UPDATE SET read_only=excluded.read_only",
        (provider, int(read_only)),
    )
    first = await scope_for(bridge, provider)
    second = replace(first, turn_id="another-turn")
    definitions = []
    for scope in (first, second):
        ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
        async with bridge.toolset(scope, scope.grants[0]) as tools:
            values = await tools.get_tools(ctx)
            assert values
            assert all(tool.args_validator_func is None for tool in values.values())
            definitions.append({
                name: (asdict(tool.tool_def), tool.max_retries) for name, tool in values.items()
            })
    assert definitions[0] == definitions[1]


async def test_connector_check_reuses_current_native_grants_and_compares_full_policy(bridge, monkeypatch):
    import copy
    from dataclasses import replace

    save(bridge, "linear")
    scope = await scope_for(bridge, "linear")
    resolve = bridge.resolve_grants
    reads = []

    async def counted(*args, **kwargs):
        reads.append(1)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(bridge, "resolve_grants", counted)
    await bridge.check(scope, scope.grants[0])
    assert len(reads) == 1
    # IDs/revisions alone do not permit a changed policy. This is the extra
    # canonical comparison previously performed by a second source-store read.
    changed = copy.deepcopy(scope.grants[0])
    changed.policy["product"]["access"] = "different-access"
    with pytest.raises(PermissionError, match="authority changed"):
        await bridge.check(replace(scope, grants=[changed]), changed)


async def test_reference_only_grants_follow_live_disconnect_reconnect_and_policy(bridge):
    save(bridge, "linear")
    scope = await scope_for(bridge, "linear")
    grant = scope.grants[0]
    assert grant.credential == ""
    assert "synthetic-token" not in json.dumps(asdict(scope))
    assert await bridge.resolve_grants(scope.actor_id, [grant.id]) == [grant]
    bridge.store.execute("UPDATE connection_policies SET enabled=0 WHERE provider='linear'")
    # Explicit policy insertion for a store without a preexisting policy row.
    bridge.store.execute(
        "INSERT INTO connection_policies(provider,enabled,read_only) VALUES('linear',0,0) "
        "ON CONFLICT(provider) DO UPDATE SET enabled=0",
    )
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    bridge.store.execute("DELETE FROM connections WHERE provider='linear'")
    save(bridge, "linear")
    bridge.store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='linear'")
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    assert (await scope_for(bridge, "linear")).grants[0].id != grant.id


async def test_refresh_preserves_authority_and_revocation_wins_while_awaiting(bridge, monkeypatch):
    save(bridge, "linear", expires_at=time.time() - 1, refresh_token="synthetic-refresh")
    scope = await scope_for(bridge, "linear")

    async def exchange(provider, **kwargs):
        return {"access_token": "synthetic-refreshed", "expires_at": time.time() + 3600}

    monkeypatch.setattr(bridge.connectors, "exchange", exchange)
    value = await bridge.credentials(scope, scope.grants[0])
    assert value["access_token"] == "synthetic-refreshed"
    await bridge.require_scope(scope)

    save(bridge, "linear", expires_at=time.time() - 1, refresh_token="synthetic-refresh")
    scope = await scope_for(bridge, "linear")

    async def revoked(provider, **kwargs):
        bridge.store.execute("DELETE FROM connections WHERE provider=?", (provider,))
        return {"access_token": "must-not-resurrect"}

    monkeypatch.setattr(bridge.connectors, "exchange", revoked)
    with pytest.raises(ConnectorError, match="changed"):
        await bridge.credentials(scope, scope.grants[0])
    assert not bridge.store.rows("SELECT 1 FROM connections WHERE provider='linear'")


def github_accounts(bridge, count=16):
    credentials = {
        "kind": "github_app", "installation_id": 1, "account_id": 1,
        "repository_ids": list(range(1, count + 1)),
    }
    bridge.connectors.save("github", credentials, "Selected repositories")
    bridge.connectors.github.save_app({"id": 1})
    return credentials


def gitlab_accounts(bridge, *, origin="https://gitlab.com", ids=(7, 8)):
    save(bridge, "gitlab", base_url=origin, repository_ids=list(ids),
         repositories=[{"id": identity, "full_name": f"group/subgroup/project-{identity}"} for identity in ids])


async def test_gitlab_viewer_credentials_use_live_selection_without_a_running_turn(bridge, monkeypatch):
    gitlab_accounts(bridge)
    scope = await scope_for(bridge, "gitlab")
    grant = scope.grants[0]

    async def no_running_turn(_scope):
        raise PermissionError("The completed chat has no running turn.")

    monkeypatch.setattr(bridge.database, "authorize_scope", no_running_turn)
    with pytest.raises(PermissionError, match="no running turn"):
        await bridge.credentials(scope, grant)
    value = await bridge.read_credentials(grant, lambda: bridge.require_scope(scope))
    assert value["repository_ids"] == [grant.policy["product"]["repository_id"]]
    assert value["access_token"] == "synthetic-token"
    assert value["base_url"] == "https://gitlab.com"


@pytest.mark.parametrize("stage", ["waiting_for_lock", "credential_read"])
@pytest.mark.parametrize("revocation", ["requester", "rotation"])
async def test_gitlab_viewer_never_returns_credentials_after_revocation(
    bridge, monkeypatch, stage, revocation,
):
    gitlab_accounts(bridge)
    scope = await scope_for(bridge, "gitlab")
    grant = scope.grants[0]
    waiting, release = asyncio.Event(), asyncio.Event()
    revoked, issued = False, []
    original = bridge._credentials
    lock = bridge.connectors.locks["gitlab"]

    async def authorize():
        if revoked:
            raise PermissionError("Viewer access was revoked.")
        await bridge.require_scope(scope)
        if stage == "waiting_for_lock":
            waiting.set()

    async def issue(selected, *, write):
        assert not write
        value = await original(selected, write=write)
        issued.append(True)
        if stage == "credential_read":
            waiting.set()
            await release.wait()
        return value

    monkeypatch.setattr(bridge, "_credentials", issue)
    if stage == "waiting_for_lock":
        await lock.acquire()
    task = asyncio.create_task(bridge.read_credentials(grant, authorize))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        if revocation == "requester":
            revoked = True
        else:
            gitlab_accounts(bridge)
        if stage == "waiting_for_lock":
            lock.release()
        release.set()
        with pytest.raises((PermissionError, HTTPException)):
            await asyncio.wait_for(task, 1)
        assert bool(issued) == (stage == "credential_read")
    finally:
        if lock.locked() and stage == "waiting_for_lock":
            lock.release()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_gitlab_local_http_requires_explicit_runtime_configuration(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv("GITLAB_ALLOW_LOOPBACK_HTTP", raising=False)
    settings = settings_for(tmp_path)
    assert not settings.gitlab_allow_loopback_http
    bridge.connectors.gitlab.settings = settings
    with pytest.raises(ConnectorError):
        bridge.connectors.gitlab.normalize_base_url("http://127.0.0.1:58990")
    monkeypatch.setenv("GITLAB_ALLOW_LOOPBACK_HTTP", "true")
    enabled = settings_for(tmp_path)
    runtime_connections = ProductConnections(
        SimpleNamespace(store_proxy=bridge.store, security=bridge.security, settings=enabled),
        bridge.database,
    )
    assert runtime_connections.connectors.gitlab.settings is enabled
    assert runtime_connections.connectors.gitlab.allow_loopback_http
    assert runtime_connections.connectors.gitlab.normalize_base_url("http://127.0.0.1:58990") == "http://127.0.0.1:58990"
    with pytest.raises(ConnectorError):
        runtime_connections.connectors.gitlab.normalize_base_url("http://gitlab.example.test")


async def test_gitlab_project_grants_bind_origin_identity_revision_and_never_contain_tokens(bridge):
    gitlab_accounts(bridge)
    scope = await scope_for(bridge, "gitlab")
    assert {grant.policy["product"]["repository_id"] for grant in scope.grants} == {7, 8}
    assert all(grant.policy["product"]["base_url"] == "https://gitlab.com" for grant in scope.grants)
    assert all(grant.kind == "gitlab" and grant.credential == "" for grant in scope.grants)
    assert "synthetic-token" not in json.dumps(asdict(scope))
    grant = scope.grants[0]
    value = await bridge.credentials(scope, grant)
    assert value["repository_ids"] == [grant.policy["product"]["repository_id"]]
    assert [item["id"] for item in value["repositories"]] == value["repository_ids"]
    with pytest.raises(ConnectorError, match="explicit project ID"):
        bridge.connectors.gitlab.target("group/subgroup/unselected")
    assert bridge.connectors.gitlab.target("group/subgroup/project-7") == 7
    assert bridge.connectors.gitlab.target("7") == 7

    for origin in ("https://gitlab.com", "https://gitlab.example.test"):
        gitlab_accounts(bridge, origin=origin)
        with pytest.raises(HTTPException):
            await bridge.require_scope(scope)
        scope = await scope_for(bridge, "gitlab")
        assert scope.grants[0].id != grant.id
        grant = scope.grants[0]


async def test_gitlab_project_selection_and_policy_revoke_old_grants(bridge):
    from app.connection_authority import set_policy

    gitlab_accounts(bridge)
    scope = await scope_for(bridge, "gitlab")
    gitlab_accounts(bridge, ids=(7,))
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    current = await scope_for(bridge, "gitlab")
    assert len(current.grants) == 1
    set_policy(bridge.store, bridge.security, "gitlab", True, True)
    with pytest.raises(HTTPException):
        await bridge.require_scope(current)
    current = await scope_for(bridge, "gitlab")
    with pytest.raises(PermissionError, match="read only"):
        await bridge.credentials(current, current.grants[0], write=True)
    bridge.store.execute("DELETE FROM connections WHERE provider='gitlab'")
    with pytest.raises(HTTPException):
        await bridge.require_scope(current)


async def test_gitlab_origin_is_independent_grant_authority(bridge):
    gitlab_accounts(bridge, ids=(7,))
    scope = await scope_for(bridge, "gitlab")
    credentials = bridge.connectors.gitlab.saved_credentials()
    credentials["base_url"] = "https://another-gitlab.example.test"
    # Even a metadata-only update retaining the credential epoch cannot move an
    # admitted numeric project ID to a different GitLab instance.
    bridge.store.execute(
        "UPDATE connections SET encrypted=? WHERE provider='gitlab'",
        (bridge.security.encrypt(json.dumps(credentials)),),
    )
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    updated = await scope_for(bridge, "gitlab")
    assert updated.grants[0].id != scope.grants[0].id


@pytest.mark.parametrize("ids", [None, [], [True], [-1], ["7"]])
async def test_gitlab_invalid_saved_selection_never_creates_provider_wide_grant(bridge, ids):
    save(bridge, "gitlab", base_url="https://gitlab.com", repository_ids=ids)
    with pytest.raises(HTTPException):
        await scope_for(bridge, "gitlab")


@pytest.mark.parametrize("origin", [
    "http://gitlab.com", "http://localhost:58990", "http://127.0.0.1.evil.test:58990",
    "https://user:token@gitlab.com", "https://gitlab.com/api/v4",
    "https://gitlab.com?token=private", "https://gitlab.com/#fragment", "https://gitlab.com\\@evil.test",
])
def test_gitlab_setup_rejects_unsafe_origins_without_including_secret(origin):
    from pydantic import ValidationError

    from moyai.product_connections import GitLabProjectDiscovery

    with pytest.raises(ValidationError):
        GitLabProjectDiscovery(token="synthetic-token", base_url=origin)


@pytest.mark.parametrize("origin,normalized", [
    ("https://GITLAB.com:443/", "https://gitlab.com"),
    ("https://gitlab.example.test:8443", "https://gitlab.example.test:8443"),
    ("http://127.0.0.1:58990", "http://127.0.0.1:58990"),
    ("http://[::1]:58990/", "http://[::1]:58990"),
])
def test_gitlab_setup_accepts_https_and_explicit_literal_loopback(origin, normalized):
    from moyai.product_connections import GitLabProjectDiscovery

    assert GitLabProjectDiscovery(token="synthetic-token", base_url=origin).base_url == normalized


@pytest.fixture
async def gitlab_settings_client(bridge, monkeypatch):
    import httpx
    from fastapi import FastAPI, Response

    from moyai.product_connections import routes

    async def authorized(*args, **kwargs):
        return None

    async def request(method, path, **kwargs):
        assert method == "GET"
        assert kwargs["token"] == "synthetic-token"
        assert kwargs["base_url"] == "https://gitlab.com"
        if path == "/projects":
            return [{"id": 7, "path_with_namespace": "group/subgroup/repo", "token": "must-not-leak"}]
        identity = int(path.rsplit("/", 1)[-1])
        return {"id": identity, "path_with_namespace": f"group/subgroup/repo-{identity}"}

    async def verify(credentials):
        return ", ".join(item["full_name"] for item in credentials["repositories"])

    bridge.product.identity = SimpleNamespace(authenticate=authorized, require_request_current=authorized)
    monkeypatch.setattr(bridge.connectors.gitlab, "request", request)
    monkeypatch.setattr(bridge.connectors.gitlab, "verify", verify)
    app = FastAPI()
    app.include_router(routes(bridge))
    response = Response()
    sid = bridge.security.new_session(response)
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=bridge.settings.public_url) as client:
        client.cookies.set("workspace_session", cookie)
        client.headers.update({"Origin": bridge.settings.public_url, "X-CSRF-Token": bridge.security.csrf(sid)})
        yield client


async def test_gitlab_onboarding_requires_selection_and_returns_safe_metadata(bridge, gitlab_settings_client):
    client = gitlab_settings_client
    discovered = await client.post("/api/connections/gitlab/projects/discover", json={"token": "synthetic-token"})
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["repositories"] == [{"id": 7, "full_name": "group/subgroup/repo"}]
    assert "must-not-leak" not in discovered.text and "synthetic-token" not in discovered.text
    assert not bridge.connectors.gitlab.saved_credentials()
    body = {"token": "synthetic-token", "repository_ids": [], "revision": discovered.json()["revision"]}
    assert (await client.post("/api/connections/gitlab/connect", json=body)).status_code == 422
    body["repository_ids"] = [7]
    connected = await client.post("/api/connections/gitlab/connect", json=body)
    assert connected.status_code == 200, connected.text
    assert "synthetic-token" not in connected.text
    assert bridge.connectors.gitlab.saved_credentials()["repository_ids"] == [7]
    scope = await scope_for(bridge, "gitlab")
    catalog = await bridge.catalog("google:owner")
    assert next(row for row in catalog if row["kind"] == "gitlab")["name"] == "group/subgroup/repo-7"
    accounts = await client.get("/api/product/connections")
    gitlab = next(row for row in accounts.json() if row["id"] == "gitlab")
    assert gitlab["base_url"] == "https://gitlab.com" and not gitlab["oauth_configured"]
    assert "synthetic-token" not in accounts.text
    assert (await client.post("/api/connections/gitlab", json={"token": "synthetic-token"})).status_code == 400
    assert (await client.post("/api/connections/gitlab/oauth")).status_code == 409
    listed = await client.get("/api/connections/gitlab/projects")
    assert listed.json()["selected_ids"] == [7]
    selected = await client.put("/api/connections/gitlab/projects", json={
        "repository_ids": [8], "revision": listed.json()["revision"],
    })
    assert selected.status_code == 200, selected.text
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    assert (await scope_for(bridge, "gitlab")).grants[0].policy["product"]["repository_id"] == 8


async def test_gitlab_onboarding_stale_discovery_cannot_replace_rotated_connection(bridge, gitlab_settings_client):
    client = gitlab_settings_client
    discovered = await client.post("/api/connections/gitlab/projects/discover", json={"token": "synthetic-token"})
    gitlab_accounts(bridge, ids=(8,))
    revision = discovered.json()["revision"]
    response = await client.post("/api/connections/gitlab/connect", json={
        "token": "synthetic-token", "repository_ids": [7], "revision": revision,
    })
    assert response.status_code == 409
    assert bridge.connectors.gitlab.saved_credentials()["repository_ids"] == [8]
    response = await client.post("/api/connections/gitlab/projects/discover", json={
        "token": "synthetic-token", "page": 2, "revision": revision,
    })
    assert response.status_code == 409


@pytest.mark.parametrize("revoke", ["connection", "requester"])
async def test_gitlab_setup_rechecks_after_provider_await_before_save(
    bridge, gitlab_settings_client, monkeypatch, revoke,
):
    client = gitlab_settings_client
    gitlab_accounts(bridge, ids=(8,))
    listed = await client.get("/api/connections/gitlab/projects")

    async def denied(*args, **kwargs):
        raise HTTPException(403, "Requester access changed.")

    async def request(method, path, **kwargs):
        if revoke == "connection":
            bridge.store.execute("DELETE FROM connections WHERE provider='gitlab'")
        else:
            bridge.product.identity.require_request_current = denied
        return {"id": 7, "path_with_namespace": "group/subgroup/repo"}

    monkeypatch.setattr(bridge.connectors.gitlab, "request", request)
    selected = await client.put("/api/connections/gitlab/projects", json={
        "repository_ids": [7], "revision": listed.json()["revision"],
    })
    assert selected.status_code == (409 if revoke == "connection" else 403)
    assert bridge.connectors.gitlab.saved_credentials().get("repository_ids", []) != [7]


async def test_gitlab_project_discovery_is_bounded_and_secret_validation_is_redacted(
    bridge, gitlab_settings_client, monkeypatch,
):
    client = gitlab_settings_client
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs["params"]))
        return [{"id": i, "path_with_namespace": f"group/subgroup/repo-{i}"} for i in range(1, 101)]

    monkeypatch.setattr(bridge.connectors.gitlab, "request", request)
    response = await client.post("/api/connections/gitlab/projects/discover", json={
        "token": "synthetic-token", "page": 3, "search": "subgroup",
    })
    assert response.status_code == 200
    assert response.json()["next_page"] == 4
    assert calls == [("GET", "/projects", {
        "membership": True, "simple": True, "per_page": 100, "page": 3,
        "order_by": "id", "sort": "asc", "search": "subgroup",
    })]
    response = await client.post("/api/connections/gitlab/projects/discover", json={
        "token": "synthetic-token", "base_url": "https://user:secret@gitlab.com",
    })
    assert response.status_code == 422
    assert "synthetic-token" not in response.text and "secret" not in response.text


@pytest.mark.parametrize("scopes,access_level,expected", [
    (["api"], 40, 200), (["read_api"], 40, 502), (["api"], 20, 502),
])
async def test_gitlab_setup_runs_real_provider_validation_over_mock_http_boundary(
    bridge, gitlab_settings_client, monkeypatch, scopes, access_level, expected,
):
    import app.gitlab as provider
    import httpx

    # Exercise the actual request and verify functions as well as the API routes.
    # Only the external network boundary is synthetic; this is not live E2E proof.
    monkeypatch.delattr(bridge.connectors.gitlab, "request")
    monkeypatch.delattr(bridge.connectors.gitlab, "verify")
    project = {
        "id": 7, "path_with_namespace": "group/subgroup/repo", "default_branch": "main",
        "visibility": "private", "archived": False,
        "permissions": {"project_access": {"access_level": access_level}},
        "web_url": "https://untrusted.example.test/do-not-follow",
        "private_field": "must-not-store",
    }
    paths = []

    def handle(request):
        assert request.headers["PRIVATE-TOKEN"] == "synthetic-token"
        assert "synthetic-token" not in str(request.url)
        assert request.method == "GET"
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "/api/v4/projects": [project],
            "/api/v4/projects/7": project,
            "/api/v4/user": {"id": 11, "username": "synthetic-user", "state": "active"},
            "/api/v4/personal_access_tokens/self": {
                "id": 21, "user_id": 11, "scopes": scopes, "active": True, "revoked": False,
            },
        }[request.url.path])

    monkeypatch.setattr(provider, "GitLabTransport", lambda **kwargs: httpx.MockTransport(handle))
    client = gitlab_settings_client
    discovered = await client.post("/api/connections/gitlab/projects/discover", json={"token": "synthetic-token"})
    assert discovered.status_code == 200, discovered.text
    connected = await client.post("/api/connections/gitlab/connect", json={
        "token": "synthetic-token", "repository_ids": [7], "revision": discovered.json()["revision"],
    })
    assert connected.status_code == expected, connected.text
    if expected != 200:
        assert not bridge.connectors.gitlab.saved_credentials()
        return
    credentials = bridge.connectors.gitlab.saved_credentials()
    assert credentials["repository_ids"] == [7]
    assert credentials["repositories"][0]["web_url"] == "https://gitlab.com/group/subgroup/repo"
    assert "must-not-store" not in json.dumps(credentials)
    assert "/api/v4/personal_access_tokens/self" in paths


async def test_github_token_requests_overlap_across_repositories_but_share_same_repository(bridge, monkeypatch):
    from datetime import UTC, datetime, timedelta

    github_accounts(bridge)
    scope = await scope_for(bridge, "github")
    github = bridge.connectors.github
    monkeypatch.setattr(github, "app_jwt", lambda config: "synthetic-jwt")
    active = peak = 0
    requests = []
    lock = threading.Lock()

    async def request(method, path, **kwargs):
        nonlocal active, peak
        assert method == "POST" and path == "/app/installations/1/access_tokens"
        assert kwargs["json"]["permissions"] == {"contents": "read", "pull_requests": "read"}
        repositories = kwargs["json"]["repository_ids"]
        assert len(repositories) == 1  # Never broaden the credential to all repositories.
        with lock:
            requests.append(repositories[0])
            active += 1
            peak = max(active, peak)
        try:
            await asyncio.sleep(.08)
            return {
                "token": f"repository-{repositories[0]}",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(github, "request", request)
    # Two concurrent callers for every repository exercise single-flight reuse
    # through the real invocation-local helpers and shared token cache.
    values = await asyncio.gather(*(
        bridge.credentials(scope, grant) for grant in scope.grants for _ in range(2)
    ))
    assert peak > 1
    assert sorted(requests) == list(range(1, 17))
    assert [value["access_token"] for value in values] == [
        f"repository-{grant.policy['product']['repository_id']}" for grant in scope.grants for _ in range(2)
    ]


@pytest.mark.parametrize("revoke", ["connection", "native", "policy"])
async def test_github_credentials_recheck_authority_after_parallel_requests(bridge, monkeypatch, revoke):
    github_accounts(bridge, 2)
    scope = await scope_for(bridge, "github")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def credentials(grant, *, write):
        calls.append(grant.id)
        entered.set()
        await release.wait()
        return {"access_token": "must-not-escape-after-revocation"}

    monkeypatch.setattr(bridge, "_credentials", credentials)
    pending = [asyncio.create_task(bridge.credentials(scope, grant)) for grant in scope.grants]
    await asyncio.wait_for(entered.wait(), 2)
    if revoke == "connection":
        bridge.store.execute("DELETE FROM connections WHERE provider='github'")
    elif revoke == "policy":
        bridge.store.execute(
            "INSERT INTO connection_policies(provider,enabled,read_only) VALUES('github',0,0) "
            "ON CONFLICT(provider) DO UPDATE SET enabled=0",
        )
    else:
        async def denied(scope):
            raise PermissionError("Native turn revoked")
        monkeypatch.setattr(bridge.database, "authorize_scope", denied)
    release.set()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(result, (HTTPException, PermissionError)) for result in results)


async def test_cancelled_github_credential_request_releases_only_its_repository_lock(bridge, monkeypatch):
    github_accounts(bridge, 2)
    scope = await scope_for(bridge, "github")
    entered, release = asyncio.Event(), asyncio.Event()

    async def credentials(grant, *, write):
        if grant == scope.grants[0]:
            entered.set()
            await release.wait()
        return {"access_token": grant.id}

    monkeypatch.setattr(bridge, "_credentials", credentials)
    first = asyncio.create_task(bridge.credentials(scope, scope.grants[0]))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert await asyncio.wait_for(bridge.credentials(scope, scope.grants[1]), 1) == {
            "access_token": scope.grants[1].id,
        }
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    release.set()
    assert await asyncio.wait_for(bridge.credentials(scope, scope.grants[0]), 1) == {
        "access_token": scope.grants[0].id,
    }


async def test_personal_slack_is_private_and_never_falls_back(bridge):
    save(bridge, "slack")
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    scope = await scope_for(bridge, "slack")
    assert scope.grants[0].read_only
    assert scope.grants[0].policy["product"]["personal_owner"] == scope.actor_id
    for shared, surface in [(True, "web"), (True, "automation")]:
        with pytest.raises(HTTPException):
            await bridge.resolve_grants(scope.actor_id, ["slack"], shared_session=shared, surface=surface)
    personal.disconnect(scope.actor_id)
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    assert (await scope_for(bridge, "slack")).grants[0].policy["product"]["personal_owner"] is None


@pytest.mark.parametrize("legacy_token", [False, True])
async def test_personal_metadata_refresh_preserves_inflight_native_grant(bridge, legacy_token):
    from app.connection_authority import AUTHORITY

    save(bridge, "slack")
    personal = bridge.connectors.personal_slack
    owner = "google:owner"
    personal.save(owner, {"access_token": "synthetic-personal"}, "Personal", personal.revision(owner))
    if legacy_token:
        row = personal.row(owner)
        credentials = json.loads(personal.security.decrypt(row["encrypted"]))
        credentials.pop(AUTHORITY)
        bridge.store.execute(
            "UPDATE personal_slack_grants SET encrypted=? WHERE owner_id=?",
            (personal.security.encrypt(json.dumps(credentials)), owner),
        )
    scope = await scope_for(bridge, "slack")
    row = personal.row(owner)
    credentials = json.loads(personal.security.decrypt(row["encrypted"]))
    personal.save(owner, credentials, "Verified personal account", row["revision"], refresh=True)
    assert personal.row(owner)["revision"] != row["revision"]
    await bridge.require_scope(scope)
    assert await bridge.resolve_grants(owner, [grant.id for grant in scope.grants]) == scope.grants
    personal.save(owner, credentials, "Reconnected account", personal.row(owner)["revision"])
    with pytest.raises((HTTPException, PermissionError)):
        await bridge.require_scope(scope)


async def test_canonical_notion_helpers_run_as_native_toolset_and_recheck_after_preflight(bridge, monkeypatch):
    save(bridge, "notion")
    scope = await scope_for(bridge, "notion")
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            bridge.store.execute("DELETE FROM connections WHERE provider='notion'")
            return {"id": "a" * 32, "type": "paragraph"}
        pytest.fail("Revoked Notion mutation reached the provider.")

    monkeypatch.setattr(bridge.connectors, "request", request)
    toolset = bridge.toolset(scope, scope.grants[0])
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    assert tools["query"].tool_def.metadata["annotations"]["readOnlyHint"]
    assert not tools["update_block"].tool_def.metadata["annotations"]["readOnlyHint"]
    with pytest.raises(HTTPException):
        await toolset.call_tool("update_block", {
            "block_id": "a" * 32, "block": {"type": "paragraph", "rich_text": []},
        }, ctx, tools["update_block"])
    assert len(calls) == 1


@pytest.mark.parametrize("provider", [None, "github", "gitlab"])
@pytest.mark.parametrize("read", [False, True])
async def test_connector_execution_keeps_checks_on_owner_loop_and_retains_write_evidence(
    bridge, monkeypatch, provider, read,
):
    owner = asyncio.get_running_loop()
    allowed, checks, requests = True, [], []

    async def authorize():
        assert asyncio.get_running_loop() is owner
        checks.append(allowed)
        if not allowed:
            raise PermissionError("Revoked during the provider request.")

    async def request(*args, **kwargs):
        nonlocal allowed
        assert asyncio.get_running_loop() is not owner
        requests.append(args)
        allowed = False
        return {"confirmed": True}

    target = getattr(bridge.connectors, provider) if provider else bridge.connectors
    monkeypatch.setattr(target, "request", request)

    async def operation(bound):
        transport = getattr(bound, provider) if provider else bound
        return await transport.request("GET" if read else "POST", "/synthetic")

    call = bridge.execution.call(operation, authorize=authorize, recheck=read)
    if read:
        with pytest.raises(PermissionError, match="Revoked"):
            await call
        assert checks == [True, False]
    else:
        assert await call == {"confirmed": True}
        assert checks == [True]
    # A received write remains evidence, but no later request may use its grant.
    with pytest.raises(PermissionError):
        await bridge.execution.call(operation, authorize=authorize, recheck=read)
    assert len(requests) == 1
    assert target.request is request


async def test_connector_execution_drains_repeated_cancellation_before_returning(bridge, monkeypatch):
    owner = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    completed, checks = [], []

    async def authorize():
        assert asyncio.get_running_loop() is owner
        checks.append(True)

    async def request(*_args, **_kwargs):
        owner.call_soon_threadsafe(entered.set)
        assert await asyncio.to_thread(release.wait, 5)
        completed.append(True)
        return {"complete": True}

    monkeypatch.setattr(bridge.connectors, "request", request)
    task = asyncio.create_task(bridge.execution.call(
        lambda bound: bound.request("GET", "/synthetic"), authorize=authorize, recheck=True,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not task.done() and not completed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await asyncio.wait_for(task, 5)
    assert completed == [True] and checks == [True, True]


async def test_grant_resolution_offloads_canonical_database_wait(bridge, monkeypatch):
    save(bridge, "linear")
    original = bridge._grants
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_grants", blocked)
    task = asyncio.create_task(bridge.resolve_grants("google:owner", ["linear"]))
    await entered.wait()
    await asyncio.sleep(0.01)
    assert not task.done()
    await task


async def test_reversible_policy_toggle_still_revokes_the_original_grant(bridge):
    from app.connection_authority import set_policy

    save(bridge, "linear")
    scope = await scope_for(bridge, "linear")
    set_policy(bridge.store, bridge.security, "linear", False, False)
    set_policy(bridge.store, bridge.security, "linear", True, False)
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)


async def test_public_metadata_cache_singleflight_copy_and_failed_refresh_evicts(bridge, monkeypatch):
    calls = []
    state = {"private": False, "id": 123}

    async def public(repository="", repository_id=None):
        calls.append((repository, repository_id))
        await asyncio.sleep(.01)
        return {"id": state["id"], "full_name": "owner/public", "private": state["private"]}

    monkeypatch.setattr(bridge.connectors.github, "public_repository", public)
    values = await asyncio.gather(*(bridge.public_repository(repository_id=123) for _ in range(8)))
    assert calls == [("", 123)]
    values[0]["full_name"] = "caller-mutated"
    assert (await bridge.public_repository(repository_id=123))["full_name"] == "owner/public"
    state["private"] = True
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123, refresh=True)
    assert 123 not in bridge._public_repositories
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123)
    assert len(calls) == 3
    state.update(private=False, id=456)
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123, refresh=True)
    assert not bridge._public_repositories
    state["id"] = 123
    await bridge.public_repository(repository_id=123)
    bridge._public_repositories[123] = (time.monotonic() - 301, values[1])
    await bridge.public_repository(repository_id=123)
    assert len(calls) == 6
    # Names/admission always make a fresh anonymous request, even with an ID cache.
    await bridge.public_repository("owner/public")
    await bridge.public_repository("owner/public")
    assert len(calls) == 8


async def test_onboarding_reuses_canonical_auth_csrf_and_provider_routes(bridge, monkeypatch):
    import httpx
    from fastapi import FastAPI, Response

    from moyai.product_connections import routes

    async def authorized(*args, **kwargs):
        return None

    bridge.product.identity = SimpleNamespace(
        authenticate=authorized, require_request_current=authorized,
    )
    bridge.settings.linear_client_id = "synthetic-client"
    bridge.settings.linear_client_secret = "synthetic-client-secret"

    async def verify(provider, credentials):
        return "Local " + provider

    monkeypatch.setattr(bridge.connectors, "verify", verify)
    app = FastAPI()
    app.include_router(routes(bridge))
    response = Response()
    sid = bridge.security.new_session(response)
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=bridge.settings.public_url) as client:
        assert (await client.get("/api/product/connections")).status_code == 401
        client.cookies.set("workspace_session", cookie)
        assert (await client.get("/api/product/connections")).status_code == 200
        assert (await client.post("/api/connections/linear", json={"token": "local-token"})).status_code == 403
        client.headers.update({"Origin": bridge.settings.public_url, "X-CSRF-Token": bridge.security.csrf(sid)})
        connected = await client.post("/api/connections/linear", json={"token": "local-token"})
        assert connected.status_code == 200 and "local-token" not in connected.text
        scope = await scope_for(bridge, "linear")
        oauth = await client.post("/api/connections/linear/oauth")
        assert oauth.status_code == 200 and "linear.app/oauth/authorize?" in oauth.json()["url"]
        assert (await client.delete("/api/connections/linear")).status_code == 200
        assert not bridge.store.rows("SELECT 1 FROM oauth_states WHERE provider='linear'")
        with pytest.raises(HTTPException):
            await bridge.require_scope(scope)


async def test_slack_reconnect_rechecks_blocked_profiles_without_granting_identity(bridge, monkeypatch):
    from app.connection_authority import save_if_unchanged
    bridge.settings.slack_identity_linking_enabled = True
    value = {'access_token': 'user-token', 'bot': {
        'access_token': 'bot-token', 'team': {'id': 'TTEST0001'}, 'bot_user_id': 'UBOT00001',
        'scope': 'users:read,users:read.email',
    }}
    bridge.connectors.save('slack', value, 'Previous installation')
    with bridge.store.connect() as conn:
        sender = bridge.store.slack_identity_in(conn, 'TTEST0001', 'UTEST0001')
        other = bridge.store.slack_identity_in(conn, 'TOTHER001', 'UTEST0002')
        conn.execute("UPDATE users SET profile_next_check=?,link_status='review',profile_eligible=0 WHERE kind='slack'",
                     (int(time.time()) + 300,))
    before = bridge.store.rows('SELECT * FROM users WHERE id=?', (sender,))[0]
    unrelated = bridge.store.rows('SELECT * FROM users WHERE id=?', (other,))[0]
    expected = bridge.connectors.connection_revision('slack')
    save_if_unchanged(bridge.store, bridge.security, 'slack', value, 'Reconnected', expected)
    current = bridge.store.rows('SELECT * FROM users WHERE id=?', (sender,))[0]
    assert current == {**before, 'profile_next_check': 0}
    assert bridge.store.rows('SELECT * FROM users WHERE id=?', (other,))[0] == unrelated

    async def profile(method, url, **kwargs):
        assert url.endswith('/users.info') and kwargs['params'] == {'user': 'UTEST0001'}
        return {'ok': True, 'user': {'id': 'UTEST0001', 'team_id': 'TTEST0001',
                'profile': {'email': 'owner@example.test', 'real_name': 'Verified owner'}}}

    monkeypatch.setattr(bridge.connectors, 'request', profile)
    await bridge.identities.resolve(current, 'TTEST0001')
    verified = bridge.store.rows('SELECT * FROM users WHERE id=?', (sender,))[0]
    assert verified['profile_eligible'] == 1 and verified['profile_conflict'] == 0
    assert verified['link_status'] != 'review'


async def test_superseded_slack_reconnect_does_not_change_profile_retry_state(bridge):
    from app.connection_authority import save_if_unchanged
    value = {'access_token': 'user-token', 'bot': {'access_token': 'bot-token', 'team': {'id': 'TTEST0001'}}}
    bridge.connectors.save('slack', value, 'Original')
    expected = bridge.connectors.connection_revision('slack')
    with bridge.store.connect() as conn:
        sender = bridge.store.slack_identity_in(conn, 'TTEST0001', 'UTEST0001')
        conn.execute('UPDATE users SET profile_next_check=12345 WHERE id=?', (sender,))
    bridge.connectors.save('slack', value, 'Concurrent replacement')
    with pytest.raises(ConnectorError, match='changed'):
        save_if_unchanged(bridge.store, bridge.security, 'slack', value, 'Superseded', expected)
    assert bridge.store.rows('SELECT profile_next_check FROM users WHERE id=?', (sender,))[0]['profile_next_check'] == 12345


async def test_personal_slack_keeps_bot_operations_separate_and_never_uses_user_token(bridge, monkeypatch):
    from dataclasses import replace

    from moyai.effects import requires_receipt

    save(bridge, "slack", expires_at=time.time() - 1, bot={
        "access_token": "synthetic-bot-only", "bot_user_id": "UBOT00001",
        "team": {"id": "TTEST0001"}, "scope": "chat:write",
    })
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    scope = await scope_for(bridge, "slack")
    assert {g.policy["product"]["access"] for g in scope.grants} == {"personal_reads", "bot"}
    shared = await bridge.resolve_grants(scope.actor_id, ["slack"], shared_session=True, surface="slack")
    assert len(shared) == 1 and shared[0].policy["product"]["access"] == "organization"
    bot = next(g for g in scope.grants if g.policy["product"]["access"] == "bot")
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer synthetic-bot-only"
        assert url.endswith("/chat.postMessage")
        assert kwargs["json"]["text"].startswith("Owner: ")
        calls.append(url)
        return {"ok": True, "channel": "CTEST0001", "ts": "1792100000.001"}

    monkeypatch.setattr(bridge.connectors, "request", request)
    tools = bridge.toolset(scope, bot)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    definitions = await tools.get_tools(ctx)
    assert "search" not in definitions and "send" in definitions
    assert requires_receipt(replace(definitions["send"].tool_def, name=f"slack_{bot.id[:8]}_send"))
    result = await tools.call_tool("send", {"channel": "CTEST0001", "text": "Local-only output."}, ctx, definitions["send"])
    assert result["complete"] and len(calls) == 1


def test_repository_metadata_migration_preserves_frozen_source_runs(bridge):
    credentials = {"kind": "github_app", "installation_id": 1, "account_id": 1, "repository_ids": [7]}
    bridge.connectors.save("github", credentials, "org/old")
    github = bridge.connectors.github
    github.remember_repository(
        {"id": 7, "full_name": "org/new", "owner": {"id": 1}}, credentials, alias="org/old",
    )
    run = bridge.store.create_run("Preserved history", "https://github.com/org/old", "demo", ["github"],
                                  chat_enabled=True, user_id="google:owner")
    github.source_run_mutable = lambda connection, identity: identity != run["id"]
    github.migrate_references(credentials)
    assert bridge.store.run(run["id"])["repo_url"] == "https://github.com/org/old"
    assert github.repository_name(7, credentials) == "org/new"
