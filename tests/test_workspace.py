import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.connectors import ConnectorError
from app.main import create_app
from app.security import digest


@pytest.fixture
def workspace(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url="http://127.0.0.1:8787", litellm_api_key="",
                        modal_token_id="", modal_token_secret="", demo_step_seconds=0.01)
    app = create_app(settings)
    with TestClient(app, base_url=settings.public_url, client=("127.0.0.1", 50000)) as client:
        session = client.get("/api/session").json()
        client.headers.update({"Origin": settings.public_url, "X-CSRF-Token": session["csrf"]})
        yield app, client


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


def cloud_capability(app, plugins):
    for provider in plugins:
        app.state.connectors.save(provider, {"access_token": "provider-test-token", "kind": "personal"}, "Test team")
    run = app.state.store.create_run("A controlled test task", "", "modal", plugins, model=app.state.settings.agent_model)
    token = "run-capability-only"
    app.state.store.update_run(run["id"], status="running", token_hash=digest(token))
    return run["id"], {"Authorization": f"Bearer {token}"}


def test_demo_lifecycle_is_persistent_and_honest(workspace):
    app, client = workspace
    response = client.post("/api/runs", json={"prompt": "Review a repository", "mode": "demo", "chat_enabled": False})
    assert response.status_code == 201
    run_id = response.json()["id"]
    wait_for(lambda: app.state.store.run(run_id)["status"] == "completed")
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["sandbox_id"] == ""
    assert "Demo complete" in run["summary"]
    assert len(run["events"]) == 6
    assert "token_hash" not in run
    stream = client.get(f"/api/runs/{run_id}/events").text
    assert "event: settled" in stream and "Demo complete" in stream
    resumed = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(run["events"][-1]["id"])}).text
    assert "Demo complete" not in resumed
    assert client.get("/api/runs").json()[0]["id"] == run_id


def test_auth_csrf_host_and_repository_boundaries(workspace):
    app, client = workspace
    assert client.post("/api/runs", json={"prompt": "A task"}, headers={"X-CSRF-Token": ""}).status_code == 403
    assert client.post("/api/runs", json={"prompt": "A task"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/runs", headers={"Host": "evil.example"}).status_code == 400
    for url in ["file:///etc/passwd", "https://github.com@evil.example/a/b", "https://github.com/a/b;curl", "https://github.com/a/b?token=secret"]:
        assert client.post("/api/runs", json={"prompt": "A task", "repo_url": url}).status_code == 422
    assert client.post("/api/runs", json={"prompt": "   "}).status_code == 422
    assert client.post("/api/runs", json={"prompt": "A task", "mode": "modal"}).status_code == 503
    assert client.get("/api/config").json()["cloud_ready"] is False
    client.cookies.clear()
    assert client.get("/api/runs").status_code == 401


def test_stop_run_revokes_capability(workspace):
    app, client = workspace
    app.state.settings.demo_step_seconds = 0.2
    run_id = client.post("/api/runs", json={"prompt": "A long demo"}).json()["id"]
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    wait_for(lambda: app.state.store.run(run_id)["status"] == "cancelled")
    assert not app.state.store.run(run_id)["summary"]
    run_id, headers = cloud_capability(app, ["linear"])
    assert client.get(f"/broker/{run_id}/tools", headers=headers).status_code == 200
    client.post(f"/api/runs/{run_id}/cancel")
    assert client.get(f"/broker/{run_id}/tools", headers=headers).status_code == 401


def test_tokens_are_validated_encrypted_and_never_returned(workspace, monkeypatch):
    app, client = workspace
    async def verify(provider, credentials):
        assert provider == "linear"
        return "Test organization"
    monkeypatch.setattr(app.state.connectors, "verify", verify)
    token = "lin_api_test_DO_NOT_EXPOSE"
    assert client.post("/api/connections/linear", json={"token": token}).status_code == 200
    stored = app.state.store.rows("SELECT encrypted FROM connections")[0]["encrypted"]
    assert token not in stored
    assert token not in client.get("/api/connections").text
    assert token not in client.get("/api/config").text
    assert token in app.state.security.decrypt(stored)
    assert client.delete("/api/connections/linear").status_code == 200
    assert not app.state.store.rows("SELECT * FROM connections")


@pytest.mark.parametrize("decision,expected_calls", [("approve", 1), ("deny", 0)])
@pytest.mark.parametrize('provider,name,arguments', [
    ('slack', 'slack_send', {'channel': 'C12345678', 'text': 'An approved test message'}),
    ('linear', 'linear_comment', {'issue_id': 'LIT-123', 'body': 'An approved test comment'}),
    ('notion', 'notion_append', {'page_id': 'a' * 32, 'text': 'An approved test note'}),
])
def test_writes_wait_for_exactly_one_decision(workspace, monkeypatch, decision, expected_calls, provider, name, arguments):
    app, client = workspace
    run_id, headers = cloud_capability(app, [provider])
    calls = []
    async def call(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "ts": "1234567890.123456"}
    monkeypatch.setattr(app.state.connectors, "call", call)
    body = {"name": name, "arguments": arguments}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/broker/{run_id}/tools/call", json=body, headers=headers)
        approval = wait_for(lambda: app.state.store.approvals(run_id))[-1]
        assert not calls and not future.done()
        url = f"/api/approvals/{approval['id']}"
        assert client.post(url, json={"decision": decision}).status_code == 200
        assert client.post(url, json={"decision": decision}).status_code == 409
        assert future.result(timeout=3).status_code == 200
    assert len(calls) == expected_calls
    assert app.state.store.approvals(run_id)[0]["status"] == ("completed" if expected_calls else "denied")


def test_scope_denial_and_bad_arguments_never_reach_provider(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ["linear"])
    async def fail(*args):
        pytest.fail("Provider must not be called")
    monkeypatch.setattr(app.state.connectors, "call", fail)
    assert client.post(f"/broker/{run_id}/tools/call", headers=headers, json={"name": "slack_send", "arguments": {}}).status_code == 403
    assert client.post(f"/broker/{run_id}/tools/call", headers=headers, json={"name": "linear_issue", "arguments": {"issue_id": "../../secrets"}}).status_code == 422
    assert client.get(f"/broker/{run_id}/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_uncertain_write_is_not_retried(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ["linear"])
    calls = []
    async def fail(*args):
        calls.append(1)
        raise ConnectorError("The connection dropped after sending.")
    monkeypatch.setattr(app.state.connectors, "call", fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/broker/{run_id}/tools/call", headers=headers, json={"name": "linear_comment", "arguments": {"issue_id": "LIT-123", "body": "Update"}})
        approval = wait_for(lambda: app.state.store.approvals(run_id))[-1]
        client.post(f"/api/approvals/{approval['id']}", json={"decision": "approve"})
        assert future.result(timeout=3).json()["outcome_uncertain"] is True
    assert len(calls) == 1
    assert app.state.store.approvals(run_id)[0]["status"] == "uncertain"


def test_members_can_use_org_connections_but_cannot_administer_them(workspace):
    app, client = workspace
    app.state.settings.workspace_password = "organization-admin-password"
    app.state.settings.workspace_member_password = "organization-member-password"
    app.state.connectors.save("linear", {"access_token": "not-for-the-browser", "kind": "personal"}, "Team")
    response = client.post("/api/login", json={"password": "organization-member-password"})
    assert response.json()["role"] == "member"
    session = client.get("/api/session").json()
    client.headers["X-CSRF-Token"] = session["csrf"]
    assert session["role"] == "member"
    assert client.get("/api/organization").json()["role"] == "member"
    connections = client.get("/api/connections")
    assert connections.json()[0]["scope"] == "organization"
    assert "not-for-the-browser" not in connections.text
    assert client.post("/api/runs", json={"prompt": "Member's demo task"}).status_code == 201
    for method, url, body in [
        ("post", "/api/connections/linear", {"token": "a-new-token"}),
        ("delete", "/api/connections/linear", None),
        ("post", "/api/connections/linear/oauth", None),
        ("post", "/api/connections/linear/check", None),
        ("patch", "/api/connections/linear/policy", {"enabled": False, "read_only": True}),
        ("patch", "/api/organization", {"name": "Other organization"}),
        ("post", "/api/approvals/anything", {"decision": "approve"}),
    ]:
        assert client.request(method, url, json=body).status_code == 403
    assert client.get("/oauth/linear/callback?state=unused&code=unused").status_code == 403
    app.state.settings.workspace_member_password = "a-new-member-password"
    assert client.get("/api/connections").status_code == 401


def test_org_policy_blocks_direct_tool_calls_and_survives_reconnection(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ["linear"])
    async def fail(*args):
        pytest.fail("Disabled tool reached the provider")
    monkeypatch.setattr(app.state.connectors, "call", fail)
    assert client.patch("/api/connections/linear/policy", json={"enabled": True, "read_only": True}).status_code == 200
    app.state.connectors.save("linear", {"access_token": "replacement-token", "kind": "oauth"}, "Team")
    names = {x["name"] for x in client.get(f"/broker/{run_id}/tools", headers=headers).json()}
    assert names == {"linear_teams", "linear_search", "linear_issue", "linear_my_issues"}
    assert client.post(f"/broker/{run_id}/tools/call", headers=headers,
                       json={"name": "linear_comment", "arguments": {"issue_id": "LIT-1", "body": "Blocked"}}).status_code == 403
    client.patch("/api/connections/linear/policy", json={"enabled": False, "read_only": True})
    assert client.get(f"/broker/{run_id}/tools", headers=headers).json() == []
    assert client.post(f"/broker/{run_id}/tools/call", headers=headers,
                       json={"name": "linear_search", "arguments": {"query": "MCP"}}).status_code == 403
    assert not app.state.store.approvals(run_id)
    assert len(client.get("/api/organization").json()["activity"]) == 2


def test_pausing_an_org_connection_cancels_a_waiting_write(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ["linear"])
    async def fail(*args):
        pytest.fail("A revoked approval reached the provider")
    monkeypatch.setattr(app.state.connectors, "call", fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/broker/{run_id}/tools/call", headers=headers,
                             json={"name": "linear_comment", "arguments": {"issue_id": "LIT-1", "body": "Pending"}})
        approval = wait_for(lambda: app.state.store.approvals(run_id))[0]
        client.patch("/api/connections/linear/policy", json={"enabled": False, "read_only": False})
        assert future.result(timeout=3).json()["error"] == "Action denied, expired, or cancelled."
        assert client.post(f"/api/approvals/{approval['id']}", json={"decision": "approve"}).status_code == 409
    assert app.state.store.approvals(run_id)[0]["status"] == "expired"


def test_oauth_state_is_bound_to_session_and_consumed_once(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.linear_client_id = "test-client"
    app.state.settings.linear_client_secret = "test-secret"
    result = client.post("/api/connections/linear/oauth").json()
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    url = f"/oauth/linear/callback?state={state}&code=test-code"
    saved_cookies = dict(client.cookies)
    client.cookies.clear()
    session = client.get("/api/session").json()
    assert client.get(url).status_code == 400
    client.cookies.clear()
    client.cookies.update(saved_cookies)
    async def exchange(*args, **kwargs):
        return {"access_token": "oauth-test-token", "kind": "oauth"}
    async def verify(*args):
        return "OAuth workspace"
    monkeypatch.setattr(app.state.connectors, "exchange", exchange)
    monkeypatch.setattr(app.state.connectors, "verify", verify)
    assert client.get(url, follow_redirects=False).status_code == 303
    assert client.get(url).status_code == 400


def test_restart_marks_inflight_work_without_replay(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, litellm_api_key="", modal_token_id="", modal_token_secret="")
    app = create_app(settings)
    run = app.state.store.create_run("Interrupted work", "", "modal", [])
    app.state.store.update_run(run["id"], status="running", token_hash=digest("old"))
    with TestClient(app, base_url=settings.public_url, client=("127.0.0.1", 50000)):
        row = app.state.store.run(run["id"])
        assert row["status"] == "interrupted"
        assert row["token_hash"] == ""
        assert not app.state.manager.jobs


def test_remote_workspace_requires_password(tmp_path):
    with pytest.raises(ValueError, match="WORKSPACE_PASSWORD"):
        create_app(Settings(_env_file=None, data_dir=tmp_path, public_url="https://workspace.example", workspace_password=""))


def test_model_proxy_pins_model_and_drops_gateway_overrides(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.max_agent_iterations = 30
    app.state.settings.litellm_api_base = "https://gateway.example/v1"
    app.state.settings.litellm_api_key = "server-secret"
    app.state.settings.agent_model = "approved-model"
    run_id, headers = cloud_capability(app, [])
    captured = []
    def upstream(request):
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer server-secret"
        assert str(request.url) == "https://gateway.example/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Done"}}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr("app.main.httpx.AsyncClient", lambda **kwargs: actual(transport=httpx.MockTransport(upstream), **kwargs))
    result = client.post(f"/broker/{run_id}/v1/chat/completions", headers=headers, json={"messages": [{"role": "user", "content": "Hello"}], "model": "expensive-model", "api_base": "https://evil.example", "api_key": "evil", "n": 100, "max_tokens": 999999})
    assert result.status_code == 200
    assert captured[0]["model"] == "approved-model"
    assert captured[0]["max_tokens"] == 16000
    assert not {"api_base", "api_key", "n"} & captured[0].keys()
    app.state.store.execute("UPDATE runs SET model_calls=? WHERE id=?", (app.state.settings.max_agent_iterations * 3, run_id))
    limited = client.post(f"/broker/{run_id}/v1/chat/completions", headers=headers, json={"messages": []})
    assert limited.status_code == 429
    assert len(captured) == 1

    # The unrestricted setting removes request-count limits but still accounts
    # for every call and requires an active, authenticated run.
    app.state.settings.max_agent_iterations = 0
    app.state.store.execute("UPDATE runs SET model_calls=100000,turn_model_calls=100000 WHERE id=?", (run_id,))
    assert client.post(f"/broker/{run_id}/v1/chat/completions", headers=headers, json={"messages": []}).status_code == 200
    assert app.state.store.run(run_id)['model_calls'] == 100001
    app.state.store.update_run(run_id, status='cancelled', token_hash='')
    assert client.post(f"/broker/{run_id}/v1/chat/completions", headers=headers, json={"messages": []}).status_code == 401


@pytest.mark.parametrize('user,role', [('alice', 'admin'), ('bob', 'member')])
def test_linear_issue_creation_runs_directly_for_admins_and_members(workspace, monkeypatch, user, role):
    from test_spend import sign_in
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    owner = sign_in(app, client, user, user + '@berri.ai')
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?', (owner, owner, run_id))
    assert client.get('/api/session').json()['role'] == role
    args = {'team_id':'12345678-1234-1234-1234-123456789abc','title':'Improve support agent staging',
            'description':'Discussion context and source: https://test.slack.com/archives/C12345678/p1790719000123456'}
    calls = []
    async def request(method, url, **kwargs):
        assert not app.state.store.approvals(run_id)
        assert app.state.store.run(run_id)['status'] == 'running'
        calls.append(kwargs['json'])
        return {'data':{'issueCreate':{'success':True,'issue':{'identifier':'TEST-1','url':'https://linear.app/test/TEST-1'}}}}
    monkeypatch.setattr(app.state.connectors, 'request', request)
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name':'linear_create_issue', 'arguments':args})
    assert response.status_code == 200
    assert response.json()['issueCreate']['issue']['identifier'] == 'TEST-1'
    assert len(calls) == 1
    assert calls[0]['variables']['input'] == {'teamId':args['team_id'],'title':args['title'],'description':args['description']}
    assert 'issueCreate' in calls[0]['query']
    assert not app.state.store.approvals(run_id)
    assert not any(event['kind'] == 'approval' for event in app.state.store.events(run_id))
    assert app.state.store.run(run_id)['status'] == 'running'
    connection = next(c for c in client.get('/api/connections').json() if c['id'] == 'linear')
    policy = {tool['name']: tool for tool in connection['tools']}
    assert policy['linear_create_issue']['write'] and not policy['linear_create_issue']['requires_approval']
    assert policy['linear_comment']['requires_approval']
    advertised = client.get(f'/broker/{run_id}/tools', headers=headers).json()
    tool = next(tool for tool in advertised if tool['name'] == 'linear_create_issue')
    assert tool['annotations']['readOnlyHint'] is False
    if role == 'member':
        assert client.patch('/api/connections/linear/policy', json={'enabled':True,'read_only':False}).status_code == 403
