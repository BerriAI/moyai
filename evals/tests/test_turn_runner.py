from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from agentchat.channels.slack.host import SlackHost
from moyai_harbor import turn_runner
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)


@pytest.fixture
def replay(monkeypatch):
    question = ToolCallPart(
        tool_name="ask_user_question",
        tool_call_id="question-1",
        args={"questions": [{
            "header": "Product",
            "question": "Which product?",
            "options": [
                {"label": "LiteLLM", "description": "Check LiteLLM."},
                {"label": "Moyai", "description": "Check Moyai."},
            ],
        }]},
    )
    completed = SimpleNamespace(
        id="resumed-turn", session_id="session-1", workflow_id="workflow-1",
        prompt="prompt", conversation=None, usage='{"input_tokens": 7}',
        status="waiting", error=None,
    )
    session = SimpleNamespace(active_turn=completed.id, conversation=None)
    slack_thread = SimpleNamespace(session_id="session-1")
    database_session = SimpleNamespace(scalar=AsyncMock(return_value=slack_thread))

    async def get(model, _identity):
        return session if model is turn_runner.Session else completed

    database_session.get = AsyncMock(side_effect=get)

    @asynccontextmanager
    async def sessions():
        yield database_session

    database = SimpleNamespace(
        sessions=sessions, claim_next=AsyncMock(return_value=completed),
    )
    runtime = SimpleNamespace(
        database=database,
        security=SimpleNamespace(unseal=lambda value: value.encode()),
        settings=SimpleNamespace(temporal_queue="test-queue"),
    )
    handle = SimpleNamespace(result=AsyncMock(return_value={}), cancel=AsyncMock())
    temporal = SimpleNamespace(client=SimpleNamespace(start_workflow=AsyncMock(return_value=handle)))
    posts = [{"channel": "CTEST0001", "blocks": [{
        "type": "section", "text": {"type": "mrkdwn", "text": "Moyai needs your response."},
    }]}]

    async def pump(client):
        if posts:
            await client.post("chat.postMessage", json=posts.pop(0))

    service = SimpleNamespace(receive_payload=AsyncMock(), pump=AsyncMock(side_effect=pump))
    monkeypatch.setattr(turn_runner, "pending_questions", lambda *_: SimpleNamespace(calls=[question]))
    return SimpleNamespace(
        completed=completed, session=session, runtime=runtime, temporal=temporal,
        service=service, posts=posts, handle=handle,
    )


async def execute(replay, **kwargs):
    return await turn_runner.execute_turn(
        {"id": "local-chat", "prompt": "/answer LiteLLM", "thread": []},
        trial=1, thread=[], service=replay.service,
        runtime=replay.runtime, temporal=replay.temporal, **kwargs,
    )


@pytest.mark.asyncio
async def test_dispatches_claimed_continuation_and_preserves_thread_identity(replay):
    result = await execute(replay, thread_ts="100.000001", current="100.000002", event_id="answer-2")

    replay.temporal.client.start_workflow.assert_awaited_once_with(
        turn_runner.TurnWorkflow.run, "resumed-turn", id="workflow-1", task_queue="test-queue",
    )
    assert result["slack_event_input"]["event_id"] == "answer-2"
    assert result["slack_event_input"]["event"]["thread_ts"] == "100.000001"
    assert result["slack_event_input"]["event"]["ts"] == "100.000002"
    assert result["status"] == "waiting"
    assert result["posted_reply"] == "Moyai needs your response."
    assert result["questions"][0]["id"] == "question-1"
    assert result["questions"][0]["questions"][0]["header"] == "Product"
    assert result["tokens_cost"] == {"input_tokens": 7}


@pytest.mark.asyncio
async def test_invalid_answer_keeps_pending_question_without_dispatching(replay):
    replay.runtime.database.claim_next.return_value = None
    replay.posts[:] = [{"channel": "CTEST0001", "text": "Supply one answer for each heading."}]

    result = await execute(replay)

    replay.temporal.client.start_workflow.assert_not_awaited()
    assert result["status"] == "waiting"
    assert result["questions"][0]["id"] == "question-1"
    assert result["posted_reply"] == "Supply one answer for each heading."


@pytest.mark.asyncio
async def test_control_only_reply_without_active_turn(replay):
    replay.runtime.database.claim_next.return_value = None
    replay.session.active_turn = None
    replay.posts[:] = [{"channel": "CTEST0001", "text": "This thread is listening."}]

    result = await execute(replay)

    replay.temporal.client.start_workflow.assert_not_awaited()
    assert result["status"] == "completed"
    assert result["posted_reply"] == "This thread is listening."
    assert result["questions"] == []
    assert result["tool_trace"] == []


@pytest.mark.asyncio
async def test_cancels_workflow_when_turn_wait_is_interrupted(replay):
    replay.handle.result.side_effect = TimeoutError("workflow timed out")

    with pytest.raises(TimeoutError, match="workflow timed out"):
        await execute(replay)

    replay.handle.cancel.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_slack_history_is_complete_and_retains_messages_from_stable_thread_root():
    root = "1792275000.000001"
    previous = "1792275000.000002"
    current = "1792275000.000003"
    host = SlackHost(bot_user_id="UBOT00001", workspace_id="TTEST0001")
    source = host.normalize_event({
        "type": "event_callback", "team_id": "TTEST0001", "event_id": "followup",
        "event": {
            "type": "app_mention", "user": "UTEST0001", "channel": "CTEST0001",
            "channel_type": "channel", "ts": current, "thread_ts": root, "text": "What about that model?",
        },
    })
    history = [
        {"ts": root, "user": "UTEST0001", "text": "Check DeepSeek support."},
        {"ts": previous, "user": "UBOT00001", "text": "I checked the registry."},
        {"ts": "1792275000.000004", "user": "UTEST0001", "text": "A later message must not leak in."},
    ]
    async with httpx.AsyncClient(transport=turn_runner.slack_transport(history)) as client:
        async def fetch(params):
            response = await client.get("https://slack.com/api/conversations.replies", params=params)
            return response.json()

        context = await host.thread_context(source, fetch)

    assert context["status"] == "complete"
    assert [message["ts"] for message in context["messages"]] == [root, previous]
    assert "DeepSeek support" in context["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
async def test_runner_returns_current_root_but_does_not_repeat_current_message_in_context(replay, followup):
    root = "1792275000.000001"
    current = "1792275000.000003" if followup else root
    prior = [
        {"ts": root, "role": "human", "display_name": "Local tester", "text": "First question", "files": []},
        {"ts": "1792275000.000002", "role": "you", "display_name": "Moyai", "text": "First reply", "files": []},
    ] if followup else []
    observed = []
    replies = []
    host = SlackHost(bot_user_id="UBOT00001", workspace_id="TTEST0001")

    async def receive(payload):
        source = host.normalize_event(payload)
        async with httpx.AsyncClient() as client:
            async def fetch(params):
                response = await client.get("https://slack.com/api/conversations.replies", params=params)
                page = response.json()
                replies.extend(page["messages"])
                return page

            observed.append(await host.thread_context(source, fetch))

    replay.service.receive_payload.side_effect = receive
    await turn_runner.execute_turn(
        {"id": "local-chat", "prompt": "Current question", "thread": prior},
        trial=1, thread=prior, service=replay.service,
        runtime=replay.runtime, temporal=replay.temporal, thread_ts=root, current=current,
    )

    assert observed[0]["status"] == "complete"
    assert [message["ts"] for message in observed[0]["messages"]] == [message["ts"] for message in prior]
    assert [message["ts"] for message in replies] == [*[message["ts"] for message in prior], current]
    assert sum(message["ts"] == current for message in replies) == 1
    assert all("Current question" not in message["content"] for message in observed[0]["messages"])


@pytest.mark.asyncio
async def test_current_trace_excludes_completed_history_and_keeps_resumed_question_result(replay):
    prior = [
        ModelResponse(parts=[ToolCallPart("shell", {"command": "old"}, tool_call_id="old-call")]),
        ModelRequest(parts=[ToolReturnPart("shell", "old result", tool_call_id="old-call")]),
        ModelResponse(parts=[ToolCallPart("ask_user_question", {"questions": []}, tool_call_id="question-call")]),
    ]
    replay.session.conversation = ConversationTypeAdapter.dump_json(Conversation(messages=prior)).decode()
    replay.completed.status = "completed"
    replay.completed.conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        *prior,
        ModelRequest(parts=[ToolReturnPart("ask_user_question", "LiteLLM", tool_call_id="question-call")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "new"}, tool_call_id="new-call")]),
        ModelRequest(parts=[ToolReturnPart("shell", "new result", tool_call_id="new-call")]),
    ])).decode()

    result = await execute(replay)

    assert [call["name"] for call in result["tool_trace"]] == ["ask_user_question", "shell"]
    assert result["tool_trace"][0]["result"] == '"LiteLLM"'
    assert result["tool_trace"][1]["args"] == {"command": "new"}
    assert result["tool_trace"][1]["result"] == '"new result"'
    assert len(turn_runner.conversation_trace(replay.completed.conversation, replay.runtime.security)) == 3
