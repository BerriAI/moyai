"""Supplementary native API/SDK regressions; live Modal evidence is separate."""
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import CallDeferred, Conversation, ConversationTypeAdapter, DeferredToolRequests
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select
from test_application_boundaries import sign_in
from test_shared_browser import running_browser, workspace

from moyai.api import COOKIE, create_app
from moyai.browser_login import BrowserLogin
from moyai.computer import lease
from moyai.contracts import NewMessage
from moyai.models import ComputerDesktop, Session, Turn, User, new_id
from moyai.questions import expire_optional_questions


async def waiting_login(runtime, actor, *, extra=None):
    session, scope = await running_browser(runtime, actor)
    capability = BrowserLogin(runtime.database)
    tool = capability.get_toolset().tools["request_browser_login"]
    ctx = SimpleNamespace(deps=scope, workspace=workspace())
    with pytest.raises(CallDeferred) as deferred:
        await tool.function(ctx, "https://example.test/sign-in?private=value#secret", "Inspect the protected report.")
    assert deferred.value.metadata["site"] == "https://example.test"
    call = ToolCallPart("request_browser_login", {
        "url": "https://example.test", "reason": "Inspect the protected report.",
    }, tool_call_id="login")
    pending = DeferredToolRequests(
        calls=[call, *([extra] if extra else [])], metadata={"login": deferred.value.metadata},
    )
    conversation = Conversation(
        conversation_id=scope.conversation_id,
        messages=[ModelRequest(parts=[UserPromptPart("Inspect the report.")]), ModelResponse(parts=[call])],
        deferred_tool_requests=pending,
    )
    assert await runtime.database.save_result(
        scope, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="", workspace={"provider": "modal", "id": "scoped-computer"}, usage={}, deferred=True,
    )
    return session, scope.turn_id


async def test_login_tool_defers_only_with_an_initialized_browser(runtime, active):
    _session, _turn, prepared = active
    capability = BrowserLogin(runtime.database)
    tool = capability.get_toolset().tools["request_browser_login"]
    assert tool.metadata["temporal"] is False
    assert tool.sequential
    for reference in [None, WorkspaceRef(provider="local", id="/tmp"), workspace().ref]:
        with pytest.raises(ToolFailed, match="browser_open"):
            await tool.function(SimpleNamespace(deps=prepared.scope, workspace=SimpleNamespace(ref=reference)),
                                "https://example.test", "Sign in to inspect the report.")


@pytest.mark.parametrize("url", ["file:///private", "javascript:alert(1)", "https://user:secret@example.test"])
async def test_login_tool_rejects_non_web_or_embedded_credentials(runtime, active, url):
    tool = BrowserLogin(runtime.database).get_toolset().tools["request_browser_login"]
    with pytest.raises(ToolFailed, match="HTTP"):
        await tool.function(SimpleNamespace(deps=active[2].scope, workspace=workspace()), url, "Read the report.")


async def test_request_survives_reload_and_does_not_expire_or_accept_chat(runtime, actor):
    session, turn_id = await waiting_login(runtime, actor)
    await expire_optional_questions(runtime.database)
    queued = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="I signed in", mode="asap", request_key="chat-is-not-completion",
    ))
    assert queued.deferred_results is None
    async with sign_in(runtime) as client:
        for _ in range(2):
            view = (await client.get(f"/api/sessions/{session.id}")).json()
            assert view["session"]["status"] == "waiting"
            assert view["pending"]["metadata"]["login"]["site"] == "https://example.test"
            assert view["can_answer"] is True and view["can_reply_with_text"] is False
            assert view["question_deadline"] is None
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, turn_id)).status == "waiting"


@pytest.mark.parametrize("outcome", ["signed_in", "cancelled"])
async def test_completion_resumes_once_and_never_claims_verified_authentication(runtime, actor, outcome):
    session, turn_id = await waiting_login(runtime, actor)
    body = {"turn_id": turn_id, "request_key": "login-answer", "browser_logins": {"login": outcome}}
    async with sign_in(runtime) as client:
        first = await client.post(f"/api/sessions/{session.id}/answers", json=body)
        assert first.status_code == 202, first.text
        retry = await client.post(f"/api/sessions/{session.id}/answers", json=body)
        assert retry.status_code == 202 and retry.json()["id"] == first.json()["id"]
        conflict = await client.post(f"/api/sessions/{session.id}/answers", json={**body, "request_key": "different"})
        assert conflict.status_code == 409
    async with runtime.database.sessions() as db:
        resumed = await db.get(Turn, first.json()["id"])
        answer = json.loads(runtime.security.unseal(resumed.deferred_results))["calls"]["login"]
        assert answer["status"] == ("user_reported_signed_in" if outcome == "signed_in" else "cancelled")
        assert ("browser_read" in answer["instruction"]) is (outcome == "signed_in")
        assert (await db.get(Turn, turn_id)).status == "resumed"


async def test_completion_requires_released_control_and_same_computer_but_cancel_stays_available(runtime, actor):
    session, turn_id = await waiting_login(runtime, actor)
    client_id = new_id()
    await lease(runtime, session.id, actor, "claim", client_id)
    body = {"turn_id": turn_id, "request_key": "complete", "browser_logins": {"login": "signed_in"}}
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/answers", json=body)
        assert response.status_code == 409 and "Release control" in response.text
        await lease(runtime, session.id, actor, "release", client_id)
        async with runtime.database.sessions.begin() as db:
            (await db.get(ComputerDesktop, session.id)).workspace_id = "replacement"
        response = await client.post(f"/api/sessions/{session.id}/answers", json=body)
        assert response.status_code == 409 and "computer changed" in response.text
        cancelled = await client.post(f"/api/sessions/{session.id}/answers", json={
            **body, "browser_logins": {"login": "cancelled"},
        })
        assert cancelled.status_code == 202


async def test_only_requester_can_complete_and_unknown_or_incomplete_answers_are_rejected(runtime, actor):
    question = ToolCallPart("ask_user_question", {"questions": [{
        "header": "Scope", "question": "Which report?", "options": [{"label": "Daily"}, {"label": "Weekly"}],
    }]}, tool_call_id="scope")
    session, turn_id = await waiting_login(runtime, actor, extra=question)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
        other = User(id=new_id(), username="other", display_name="Other", password="unused")
        db.add(other)
    token, csrf = runtime.security.issue_session(other.id, other.auth_version)
    body = {"turn_id": turn_id, "request_key": "complete", "browser_logins": {"login": "signed_in"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)), base_url="http://testserver",
    ) as other_client, sign_in(runtime) as client:
        other_client.cookies.set(COOKIE, token)
        other_client.headers["X-Moyai-CSRF"] = csrf
        view = (await other_client.get(f"/api/sessions/{session.id}")).json()
        assert view["can_answer"] is False and not view.get("pending")
        assert (await other_client.post(f"/api/sessions/{session.id}/answers", json=body)).status_code == 403
        for update in [{}, {"browser_logins": {"unknown": "signed_in"}}, {"browser_logins": {"login": "success"}}]:
            response = await client.post(f"/api/sessions/{session.id}/answers", json={**body, **update})
            assert response.status_code == 422
        body["answers"] = {"scope": {"answers": [{"header": "Scope", "selected": ["Daily"]}]}}
        response = await client.post(f"/api/sessions/{session.id}/answers", json=body)
        assert response.status_code == 202, response.text
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(Turn).where(Turn.request_key == "complete")))) == 1
