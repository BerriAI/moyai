"""Supplementary native PR HTTP/ACL tests, not actual-app/model E2E proof."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import event

from moyai import pull_requests
from moyai.contracts import NewMessage, NewSession
from moyai.links import LinkInput, save_link
from moyai.models import Connection, Message, Session, User, new_id
from moyai.pull_requests import PullRequestService, Scope, parse_pr_url, routes, summary, web_link

URL = "https://github.com/BerriAI/moyai/pull/465"
PR = parse_pr_url(URL)
HEAD = "a" * 40
BASE = "b" * 40


def record(**changes):
    return {
        "number": 465, "title": "Native PR viewer", "state": "open", "draft": False,
        "merged": False, "body": "Description", "user": {"login": "author", "type": "User"},
        "head": {"label": "author:feature", "sha": HEAD},
        "base": {"label": "BerriAI:main", "sha": BASE},
        "additions": 2, "deletions": 1, "changed_files": 1, "commits": 1, **changes,
    }


@asynccontextmanager
async def client_for(runtime, user, service):
    app = FastAPI()

    async def actor():
        return await runtime.database.user(user.id)

    app.include_router(routes(runtime, actor, service=service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def add_connection(runtime, user, **changes):
    async with runtime.database.sessions.begin() as db:
        row = Connection(
            id=new_id(), owner_id=user.id, kind="github", name="Test account",
            credential=runtime.security.seal("test-token-only"), **changes,
        )
        db.add(row)
    return row


async def teammate(runtime):
    async with runtime.database.sessions.begin() as db:
        user = User(id=new_id(), username=new_id(), display_name="Teammate", password="unused")
        db.add(user)
    return user


@pytest.mark.parametrize("url", [
    "http://github.com/org/repo/pull/1", "https://github.com.evil.test/org/repo/pull/1",
    "https://user:password@github.com/org/repo/pull/1", "https://github.com:8443/org/repo/pull/1",
    "https://localhost/org/repo/pull/1", "https://api.github.com/repos/org/repo/pulls/1",
    "https://github.com/../repo/pull/1", "https://github.com/org/../pull/1",
    "https://github.com/org/%2e%2e/pull/1", "https://github.com/org/repo/pull/1/files",
    "https://github.com/org/repo/pull/0", "https://github.com/org/repo/pull/999999999999",
    "https://github.com/org/repo/pull/1\nHost: evil.test",
])
def test_url_validation(url):
    with pytest.raises(HTTPException) as error:
        parse_pr_url(url)
    assert error.value.status_code == 422


def test_normalization_and_real_states():
    assert parse_pr_url(URL + "?tab=files#diff").url == PR.url
    assert parse_pr_url(URL.replace("github.com", "GITHUB.COM:443")).url == PR.url
    for data, state, draft in [
        (record(), "open", False), (record(draft=True), "open", True),
        (record(state="closed"), "closed", False),
        (record(state="closed", merged=True), "merged", False),
        (record(state="closed", merged_at="2026-10-15T01:00:00Z"), "merged", False),
    ]:
        value = summary(PR, data)
        assert (value["state"], value["draft"], value["accessible"]) == (state, draft, True)
    assert summary(PR, error="unavailable")["state"] == "unknown"


@pytest.mark.parametrize("userinfo", ["user", "user:password", ":password", "user:", ":", ""])
def test_outgoing_links_reject_any_userinfo_even_empty_username(userinfo):
    assert web_link(f"https://{userinfo}@github.com/org/repo/pull/1") is None
    assert web_link("https://github.com/org/repo/pull/1") == "https://github.com/org/repo/pull/1"
    assert web_link("https://github.com/org/repo/pull/1#discussion@123") is not None


async def test_status_batch_acl_public_messages_links_and_no_private_context(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    other = await teammate(runtime)
    private = await runtime.database.create_session(other, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt=URL, request_key="public-pr",
    ))
    async with runtime.database.sessions.begin() as db:
        # Nonpublic roles cannot become discoverable even if inserted as a Message.
        db.add(Message(
            session_id=session.id, turn_id=turn.id, role="tool",
            content=runtime.security.seal("https://github.com/private/context/pull/99"),
            identity=new_id(),
        ))
        row = await db.get(Session, session.id)
        row.conversation = runtime.security.seal("https://github.com/private/memory/pull/98")
    await save_link(runtime.database, actor, session.id, LinkInput(kind="pull_request", url=URL))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=record())

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    async with client_for(runtime, actor, service) as client:
        response = await client.post("/api/pull-requests/statuses", json={
            "session_ids": [session.id, private.id, "unknown", session.id],
        })
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        assert list(value) == [session.id]
        assert len(value[session.id]) == 1
        assert value[session.id][0]["origin"] == "attached"
        assert value[session.id][0]["state"] == "open"
        assert len(calls) == 1 and calls[0].url.path == PR.path
        denied = await client.get(f"/api/sessions/{private.id}/pull-request", params={"url": URL})
        assert denied.status_code == 404 and len(calls) == 1
        assert (await client.post("/api/pull-requests/statuses", json={"session_ids": ["x"] * 101})).status_code == 422
        assert (await client.get(f"/api/sessions/{session.id}/pull-request", params={
            "url": "https://evil.test/data",
        })).status_code == 422


async def test_link_discovery_updates_without_updated_at_and_deduplicates_across_sessions(runtime, actor):
    first = await runtime.database.create_session(actor, NewSession())
    second = await runtime.database.create_session(actor, NewSession())
    calls = []
    service = PullRequestService(runtime, transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=record()),
    ))
    assert await service.statuses(actor, [first.id]) == {first.id: []}
    await save_link(runtime.database, actor, first.id, LinkInput(kind="pull_request", url=URL))
    await save_link(runtime.database, actor, second.id, LinkInput(kind="pull_request", url=URL))
    assert (await runtime.database.session(actor, first.id)).updated_at == first.updated_at
    statements = []

    def track(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(runtime.database.engine.sync_engine, "before_cursor_execute", track)
    try:
        value = await service.statuses(actor, [first.id, second.id])
    finally:
        event.remove(runtime.database.engine.sync_engine, "before_cursor_execute", track)
    assert all(len(items) == 1 for items in value.values()) and len(calls) == 1
    assert len(statements) <= 8  # Two authority snapshots + two ranked discovery reads.
    await service.statuses(actor, [first.id, second.id])
    assert len(calls) == 1


async def test_available_connections_shared_session_rules_and_rotation(runtime, actor):
    private = await runtime.database.create_session(actor, NewSession(visibility="private"))
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    other = await teammate(runtime)
    await add_connection(runtime, other)  # Never usable by this actor.
    own = await add_connection(runtime, actor)
    calls = []

    def handler(request):
        authorization = request.headers.get("authorization")
        calls.append(authorization)
        return httpx.Response(200, json=record()) if authorization else httpx.Response(404, json={"message": "hidden"})

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    assert (await service.detail(actor, private.id, URL, "overview", False))["accessible"]
    assert calls == [None, "Bearer test-token-only"]
    assert not (await service.detail(actor, shared.id, URL, "overview", False))["accessible"]
    assert calls[-1] is None
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Connection, own.id)
        row.credential = runtime.security.seal("rotated-test-token")
        row.revision += 1
    assert (await service.detail(actor, private.id, URL, "overview", False))["accessible"]
    assert calls[-1] == "Bearer rotated-test-token"
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, own.id)).enabled = False
    value = await service.detail(actor, private.id, URL, "overview", False)
    assert not value["accessible"] and value["state"] == "unknown" and "connection" in value["error"]
    assert "token" not in json.dumps(value) and "Authorization" not in json.dumps(value)


@pytest.mark.parametrize("restriction", ["policy", "slack", "disabled", "other_user"])
async def test_ineligible_credentials_are_never_sent(runtime, actor, restriction):
    owner = await teammate(runtime) if restriction == "other_user" else actor
    connection = await add_connection(runtime, owner)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    async with runtime.database.sessions.begin() as db:
        if restriction == "policy":
            (await db.get(Connection, connection.id)).policy = '{"allowed_tools":["search_code"]}'
        if restriction == "disabled":
            (await db.get(Connection, connection.id)).enabled = False
        if restriction == "slack":
            (await db.get(Session, session.id)).surface = "slack"
    requests = []
    service = PullRequestService(runtime, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(404),
    ))
    assert not (await service.detail(actor, session.id, URL, "overview", False))["accessible"]
    assert len(requests) == 1 and "authorization" not in requests[0].headers


@pytest.mark.parametrize("revoke", ["connection", "session", "user"])
async def test_access_revoked_during_request_never_returns_old_private_response(runtime, actor, revoke):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    connection = await add_connection(runtime, actor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if "authorization" not in request.headers:
            return httpx.Response(404)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=record(title="Secret result"))

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(service.detail(actor, session.id, URL, "overview", False))
    await asyncio.wait_for(entered.wait(), 5)
    async with runtime.database.sessions.begin() as db:
        if revoke == "connection":
            (await db.get(Connection, connection.id)).enabled = False
        elif revoke == "session":
            (await db.get(Session, session.id)).deleted = True
        else:
            (await db.get(User, actor.id)).enabled = False
    release.set()
    with pytest.raises(HTTPException) as error:
        await task
    assert error.value.status_code == {"connection": 409, "session": 404, "user": 401}[revoke]


async def test_redirects_provider_errors_and_rate_limits_are_safe(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    calls = []
    for code, expected in [(302, "moved"), (404, "connection"), (429, "rate limited"), (500, "unavailable")]:
        def handler(request, status=code):
            calls.append(str(request.url))
            return httpx.Response(status, headers={"Location": "http://127.0.0.1:9000/secrets"},
                                  json={"message": "secret-do-not-reflect"})
        service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
        result = await service.detail(actor, session.id, URL, "overview", False)
        assert expected in result["error"]
        assert result["state"] == "unknown" and not result["accessible"]
        assert "secret-do-not-reflect" not in json.dumps(result)
    assert len(calls) == 4 and all(url.startswith("https://api.github.com/repos/") for url in calls)


async def test_cache_single_flight_bounds_refresh_and_actor_isolation(runtime, actor):
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=record(title=f"Fetch {len(calls)}"))

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler), max_entries=2, max_bytes=10_000)
    scope = Scope("actor-one", ())
    values = await asyncio.gather(*(service.get(scope, PR.path) for _ in range(12)))
    assert len(calls) == 1 and all(value.value["title"] == "Fetch 1" for value in values)
    values[0].value["title"] = "mutated"
    assert (await service.get(scope, PR.path)).value["title"] == "Fetch 1"
    await service.get(scope, PR.path, True)
    await service.get(Scope("actor-two", ()), PR.path)
    await service.get(scope, PR.path + "/files")
    assert len(calls) == 4 and len(service.cache) <= 2 and service.cache_bytes <= service.max_bytes
    service.ttl = 0
    await service.get(scope, PR.path, True)
    await service.get(scope, PR.path)
    assert len(calls) == 6


@pytest.mark.parametrize("with_connection", [False, True])
async def test_public_status_polling_uses_five_minute_cache_and_refresh_bypasses(
    runtime, actor, monkeypatch, with_connection,
):
    clock = [0.0]
    # Replace this module's clock reference, not asyncio's global monotonic clock.
    monkeypatch.setattr(pull_requests, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    session = await runtime.database.create_session(actor, NewSession())
    await save_link(runtime.database, actor, session.id, LinkInput(kind="pull_request", url=URL))
    if with_connection:
        await add_connection(runtime, actor)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=record(title=f"Fetch {len(calls)}"))

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    for elapsed in range(0, 3600, 20):
        clock[0] = elapsed
        assert (await service.statuses(actor, [session.id]))[session.id][0]["accessible"]
    assert len(calls) == 12  # One public read every 5 minutes, not 180 per hour.
    assert all("authorization" not in request.headers for request in calls)
    refreshed = await service.detail(actor, session.id, URL, "overview", True)
    assert refreshed["title"] == "Fetch 13" and len(calls) == 13
    assert (await service.statuses(actor, [session.id]))[session.id][0]["title"] == "Fetch 13"
    assert len(calls) == 13


@pytest.mark.parametrize("ttl", [0, 2, 15])
async def test_explicit_ttl_overrides_public_default(runtime, monkeypatch, ttl):
    clock = [0.0]
    monkeypatch.setattr(pull_requests, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    service = PullRequestService(runtime, ttl=ttl, transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json=record()),
    ))
    scope = Scope("injected-ttl", ())
    await service.get(scope, PR.path)
    if ttl:
        clock[0] = ttl - 0.01
        await service.get(scope, PR.path)
        assert len(calls) == 1
    clock[0] = ttl
    await service.get(scope, PR.path)
    assert len(calls) == 2


async def test_authenticated_revalidation_does_not_repeat_anonymous_probe(runtime, actor, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pull_requests, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    connection = await add_connection(runtime, actor)
    calls = []

    def handler(request):
        authorization = request.headers.get("authorization")
        calls.append(authorization)
        return httpx.Response(200, json=record()) if authorization else httpx.Response(404)

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    await service.detail(actor, session.id, URL, "overview", False)
    assert calls == [None, "Bearer test-token-only"]
    clock[0] = 14
    await service.detail(actor, session.id, URL, "overview", False)
    assert len(calls) == 2
    clock[0] = 15
    await service.detail(actor, session.id, URL, "overview", False)
    assert calls == [None, "Bearer test-token-only", "Bearer test-token-only"]
    await service.detail(actor, session.id, URL, "overview", True)
    assert calls[-1] == "Bearer test-token-only" and len(calls) == 4
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, connection.id)).revision += 1
    await service.detail(actor, session.id, URL, "overview", False)
    assert calls[-2:] == [None, "Bearer test-token-only"]


async def test_diff_partial_binary_pagination_and_revision_fencing(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    current = record(changed_files=4)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, headers={"Link": '<http://evil.test/next>; rel="next"'}, json=[
                {"filename": "complete.py", "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"},
                {"filename": "partial.py", "additions": 99, "deletions": 0, "patch": "@@ -0 +1 @@\n+only"},
                {"filename": "image.png", "additions": 0, "deletions": 0},
            ])
        return httpx.Response(200, json=current)

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    value = await service.detail(actor, session.id, URL, "files", False)
    section = value["section"]
    assert section["limited"] and [f["patch_status"] for f in section["items"]] == ["available", "partial", "unavailable"]
    assert all(not path.startswith("http") for path in calls)
    # The same file URL under a new SHA must not reuse the old file page.
    current = record(head={"sha": "c" * 40}, changed_files=4)
    await service.detail(actor, session.id, URL, "overview", True)
    await service.detail(actor, session.id, URL, "files", False)
    assert calls.count(PR.path + "/files") == 2

    def changed_handler(request):
        if request.url.path.endswith("/files"):
            current["head"] = {"sha": "d" * 40}
            return httpx.Response(200, json=[{"filename": "stale.py", "patch": "+stale"}])
        return httpx.Response(200, json=current)

    service = PullRequestService(runtime, transport=httpx.MockTransport(changed_handler))
    value = await service.detail(actor, session.id, URL, "files", True)
    assert value["section"]["items"] == [] and "changed while loading" in value["section"]["error"]


async def test_discussion_bot_feedback_partial_checks_and_commits(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())

    def handler(request):
        path = request.url.path
        if path.endswith("/reviews"):
            return httpx.Response(200, json=[{
                "id": 1, "user": {"login": "reviewer[bot]", "type": "Bot"}, "body": "Finding: use a safe URL",
                "state": "COMMENTED", "submitted_at": "2026-10-15T01:00:00Z",
            }])
        if path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if path.endswith("/check-runs"):
            return httpx.Response(403, json={"message": "token=do-not-expose"})
        if path.endswith("/status"):
            assert HEAD in path
            return httpx.Response(200, json={"statuses": [
                {"id": 2, "context": "CI", "state": "pending", "target_url": "javascript:alert(1)"},
            ]})
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{
                "sha": HEAD, "commit": {"message": "Implement viewer"}, "author": {"login": "author"},
            }])
        return httpx.Response(200, json=record())

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    discussion = (await service.detail(actor, session.id, URL, "discussion", False))["section"]
    assert discussion["items"][0]["author"]["bot"] and discussion["items"][0]["body"].startswith("Finding")
    checks = (await service.detail(actor, session.id, URL, "checks", False))["section"]
    assert checks["error"] and checks["items"][0]["status"] == "pending"
    assert checks["items"][0]["url"] is None and "do-not-expose" not in json.dumps(checks)
    commits = (await service.detail(actor, session.id, URL, "commits", False))["section"]
    assert commits["items"][0]["sha"] == HEAD


async def test_public_reads_ignore_machine_credentials_and_bound_large_responses(runtime, actor, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "machine-token-must-not-be-used")
    monkeypatch.setenv("GH_TOKEN", "another-machine-token")
    session = await runtime.database.create_session(actor, NewSession())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=record(body="x" * (3 * 1024 * 1024)))

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    value = await service.detail(actor, session.id, URL, "overview", False)
    assert not value["accessible"] and "too large" in value["error"]
    assert len(requests) == 1 and "authorization" not in requests[0].headers


async def test_check_findings_and_display_limits_are_explicit(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())

    def handler(request):
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=[{
                "filename": "large.txt", "additions": 1500, "deletions": 0, "patch": "\n".join(["+line"] * 1500),
            }])
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"total_count": 102, "check_runs": [{
                "id": 1, "name": "Review bot", "status": "completed", "conclusion": "failure",
                "output": {"title": "Finding", "summary": "Unsafe URL", "text": "x" * 60001, "annotations_count": 2},
            }]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"total_count": 0, "statuses": []})
        return httpx.Response(200, json=record())

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    files = (await service.detail(actor, session.id, URL, "files", False))["section"]
    assert len(files["items"][0]["patch"].splitlines()) == 1000
    assert files["items"][0]["patch_status"] == "partial"
    checks = (await service.detail(actor, session.id, URL, "checks", False))["section"]
    assert checks["limited"] and checks["items"][0]["output_limited"]
    assert checks["items"][0]["status"] == "failure" and checks["items"][0]["annotations"] == 2


async def test_status_cache_cannot_cross_viewer_or_revoked_session_acl(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    other = await teammate(runtime)
    await add_connection(runtime, actor, shared=True)
    await save_link(runtime.database, actor, session.id, LinkInput(kind="pull_request", url=URL))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=record()) if "authorization" in request.headers else httpx.Response(404)

    service = PullRequestService(runtime, transport=httpx.MockTransport(handler))
    assert (await service.statuses(actor, [session.id]))[session.id][0]["accessible"]
    assert (await service.statuses(other, [session.id]))[session.id][0]["accessible"]
    assert len(calls) == 4  # Never reuse another actor's cached private record.
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "private"
    assert await service.statuses(other, [session.id]) == {}
    assert len(calls) == 4
