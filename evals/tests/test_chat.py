from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from moyai_harbor.chat import ChatConfig, LocalChat, create_app


class RecordingChat(LocalChat):
    def __init__(self, config, backend_factory):
        super().__init__(config, backend_factory=backend_factory)
        self.calls = []
        self.results = []
        self.release = asyncio.Event()
        self.release.set()

    async def execute(self, case, **kwargs):
        self.calls.append((case, kwargs))
        await self.release.wait()
        result = self.results.pop(0) if self.results else {"posted_reply": "Checked the model."}
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
async def chat_client(tmp_path):
    backend = SimpleNamespace(service=object(), runtime=object(), temporal=object())

    @asynccontextmanager
    async def factory(_config):
        yield backend

    controller = RecordingChat(ChatConfig(model="test-model", data_dir=tmp_path), factory)
    app = create_app(controller)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:8798",
            headers={"X-Moyai-Local": "1"},
        ) as client:
            yield controller, client


async def new_chat(client):
    response = await client.post("/api/chats")
    assert response.status_code == 200
    return response.json()["id"]


async def send(client, identity, message="Do we support DeepSeek?", request_id="request-1", **kwargs):
    return await client.post(
        f"/api/chats/{identity}/messages",
        json={"message": message, "request_id": request_id, **kwargs},
    )


async def finish(controller):
    assert controller.active is not None
    await asyncio.wait_for(controller.active, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1:8798", "localhost:8798", "[::1]:8798"])
async def test_loopback_hosts_and_read_security_headers(chat_client, host):
    _controller, client = chat_client
    response = await client.get("/api/status", headers={"Host": host})

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["model"] == "test-model"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.asyncio
async def test_rejects_non_loopback_host_even_with_matching_origin(chat_client):
    controller, client = chat_client
    response = await client.post(
        "/api/chats", headers={"Host": "attacker.example", "Origin": "http://attacker.example"},
    )

    assert response.status_code == 400
    assert controller.chats == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://localhost:8798"])
async def test_rejects_cross_origin_writes(chat_client, origin):
    controller, client = chat_client
    response = await client.post("/api/chats", headers={"Origin": origin})

    assert response.status_code == 403
    assert controller.chats == {}


@pytest.mark.asyncio
async def test_requires_local_write_header_and_accepts_same_origin(chat_client):
    controller, client = chat_client
    client.headers.pop("X-Moyai-Local")
    denied = await client.post("/api/chats", headers={"Origin": "http://127.0.0.1:8798"})
    assert denied.status_code == 403
    assert controller.chats == {}

    accepted = await client.post(
        "/api/chats", headers={"Origin": "http://127.0.0.1:8798", "X-Moyai-Local": "1"},
    )
    assert accepted.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("length, status", [("100001", 413), ("invalid", 400)])
async def test_rejects_invalid_or_oversized_content_length(chat_client, length, status):
    controller, client = chat_client
    response = await client.post("/api/chats", headers={"Content-Length": length})

    assert response.status_code == status
    assert controller.chats == {}


@pytest.mark.asyncio
async def test_request_replay_is_idempotent_and_busy_guard_is_global(chat_client):
    controller, client = chat_client
    first, second = await new_chat(client), await new_chat(client)
    controller.release.clear()

    accepted = await send(client, first)
    replay = await send(client, first)
    changed = await send(client, first, message="Different message")
    busy = await send(client, first, request_id="request-2")
    other_chat = await send(client, second)

    assert accepted.status_code == replay.status_code == 202
    assert accepted.json()["messages"] == replay.json()["messages"]
    assert changed.status_code == busy.status_code == other_chat.status_code == 409
    assert len(controller.calls) == 1
    assert controller.chats[second].messages == []

    controller.release.set()
    await finish(controller)
    completed_replay = await send(client, first)
    assert completed_replay.status_code == 202
    assert completed_replay.json()["status"] == "idle"
    assert len(controller.calls) == 1
    assert len(controller.chats[first].messages) == 2


@pytest.mark.asyncio
async def test_followup_uses_same_thread_and_reload_restores_history(chat_client):
    controller, client = chat_client
    identity = await new_chat(client)
    assert (await send(client, identity, message="First question")).status_code == 202
    await finish(controller)
    first_messages = (await client.get(f"/api/chats/{identity}")).json()["messages"]
    assert (await send(client, identity, message="Can you verify?", request_id="request-2")).status_code == 202
    await finish(controller)

    first_case, first_kwargs = controller.calls[0]
    second_case, second_kwargs = controller.calls[1]
    assert first_case["thread"] == []
    assert first_messages[0]["ts"] == first_kwargs["current"] == first_kwargs["thread_ts"]
    assert first_kwargs["thread"][0]["ts"] == first_kwargs["thread_ts"]
    assert first_messages[1]["ts"] > first_messages[0]["ts"]
    assert second_case["thread"] == [
        {"role": "human", "display_name": "Local tester", "text": "First question", "files": [],
         "ts": first_messages[0]["ts"]},
        {"role": "you", "display_name": "Moyai", "text": "Checked the model.", "files": [],
         "ts": first_messages[1]["ts"]},
    ]
    assert second_kwargs["thread"][:-1] == second_case["thread"]
    assert first_kwargs["thread_ts"] == second_kwargs["thread_ts"] == controller.chats[identity].thread_ts
    assert first_kwargs["event_id"] != second_kwargs["event_id"]
    assert first_messages[1]["ts"] < second_kwargs["current"]
    assert second_kwargs["thread"][-1]["text"] == "Can you verify?"
    assert second_kwargs["thread"][-1]["ts"] == second_kwargs["current"]
    assert second_kwargs["trial"] == 2

    reloaded = await client.get(f"/api/chats/{identity}")
    assert reloaded.status_code == 200
    assert reloaded.json()["status"] == "idle"
    assert reloaded.json()["messages"][:2] == first_messages
    assert reloaded.json()["messages"][2]["ts"] == second_kwargs["current"]
    assert reloaded.json()["messages"][3]["ts"] > second_kwargs["current"]
    assert [item["text"] for item in reloaded.json()["messages"]] == [
        "First question", "Checked the model.", "Can you verify?", "Checked the model.",
    ]
    assert (await client.get("/api/chats")).json()["chats"] == [reloaded.json()]
    transcript = controller.config.data_dir / "transcripts" / f"{identity}.json"
    assert json.loads(transcript.read_text()) == reloaded.json()


@pytest.mark.asyncio
async def test_pending_questions_are_mapped_and_json_answers_resume_the_thread(chat_client):
    controller, client = chat_client
    question = {
        "header": "Product", "question": "Which product?",
        "options": [{"label": "LiteLLM", "description": "Check LiteLLM."}],
    }
    controller.results.append({"status": "waiting", "questions": [{"id": "q-1", "questions": [question]}]})
    identity = await new_chat(client)
    await send(client, identity)
    await finish(controller)
    waiting = (await client.get(f"/api/chats/{identity}")).json()
    assert waiting["status"] == "waiting"
    assert waiting["messages"][-1]["questions"] == [question]
    assert waiting["messages"][-1]["text"] == "Which product?"

    answered = await send(client, identity, message="", request_id="answer-1", answers={"Product": "LiteLLM"})
    assert answered.status_code == 202
    await finish(controller)
    case, kwargs = controller.calls[-1]
    assert case["prompt"] == '/answer {"Product": "LiteLLM"}'
    assert kwargs["thread_ts"] == controller.calls[0][1]["thread_ts"]
    assert kwargs["thread"][-1]["text"] == case["prompt"]
    assert controller.chats[identity].messages[-2]["text"] == "Product: LiteLLM"
    assert controller.chats[identity].status == "idle"


@pytest.mark.asyncio
async def test_plain_followup_answers_a_pending_question(chat_client):
    controller, client = chat_client
    controller.results.append({"status": "waiting", "posted_reply": "Which product?"})
    identity = await new_chat(client)
    await send(client, identity)
    await finish(controller)
    await send(client, identity, message="LiteLLM", request_id="answer-1")
    await finish(controller)

    assert controller.calls[-1][0]["prompt"] == "/answer LiteLLM"


@pytest.mark.asyncio
async def test_invalid_messages_and_answers_do_not_consume_request_ids(chat_client):
    controller, client = chat_client
    identity = await new_chat(client)
    assert (await send(client, identity, message="   ")).status_code == 422
    assert (await send(client, identity, answers={"Product": "LiteLLM"})).status_code == 409
    assert controller.chats[identity].requests == {}
    assert controller.chats[identity].messages == []

    controller.results.append({"status": "waiting", "posted_reply": "Which product?"})
    assert (await send(client, identity)).status_code == 202
    await finish(controller)
    invalid = await send(client, identity, request_id="answer-1", answers={"Product": " "})
    assert invalid.status_code == 422
    assert controller.chats[identity].status == "waiting"
    assert "answer-1" not in controller.chats[identity].requests


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "result"])
async def test_failed_turn_is_visible_redacted_and_next_chat_is_usable(chat_client, monkeypatch, failure):
    controller, client = chat_client
    monkeypatch.setenv("LITELLM_API_KEY", "secret-for-test")
    error = "Gateway rejected secret-for-test"
    controller.results.append(RuntimeError(error) if failure == "exception" else {"execution_error": error})
    identity = await new_chat(client)
    await send(client, identity)
    await finish(controller)

    failed = (await client.get(f"/api/chats/{identity}")).json()
    assert failed["status"] == "error"
    assert failed["error"] == "Gateway rejected [redacted]"
    assert failed["messages"][-1]["status"] == "error"
    assert "secret-for-test" not in json.dumps(failed)

    next_chat = await new_chat(client)
    assert (await send(client, next_chat)).status_code == 202
    await finish(controller)
    assert controller.chats[next_chat].status == "idle"
    assert controller.chats[next_chat].messages[-1]["text"] == "Checked the model."


@pytest.mark.asyncio
async def test_unknown_chat_is_not_found(chat_client):
    _controller, client = chat_client
    assert (await client.get("/api/chats/not-a-chat")).status_code == 404
    assert (await send(client, "not-a-chat")).status_code == 404


@pytest.mark.asyncio
async def test_startup_failure_is_exposed_and_sending_is_unavailable(tmp_path):
    @asynccontextmanager
    async def broken_factory(_config):
        raise RuntimeError("The local test database is unavailable.")
        yield  # pragma: no cover - this is an async context manager that fails to enter

    controller = RecordingChat(ChatConfig(model="test-model", data_dir=tmp_path), broken_factory)
    app = create_app(controller)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8798",
            headers={"X-Moyai-Local": "1"},
        ) as client:
            status = (await client.get("/api/status")).json()
            assert status["phase"] == "error"
            assert status["ready"] is False
            assert status["error"] == "The local test database is unavailable."
            identity = await new_chat(client)
            response = await send(client, identity)
            assert response.status_code == 503
            assert response.json()["detail"] == status["error"]
            assert controller.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/stop", "/sleep", "STOP", "sleep"])
async def test_unsupported_stop_controls_do_not_mutate_chat(chat_client, command):
    controller, client = chat_client
    identity = await new_chat(client)

    response = await send(client, identity, message=command)

    assert response.status_code == 422
    assert "New chat" in response.json()["detail"]
    assert controller.chats[identity].messages == []
    assert controller.chats[identity].requests == {}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_timeout_has_actionable_message_even_when_exception_text_is_empty(chat_client):
    controller, client = chat_client
    controller.results.append(TimeoutError())
    identity = await new_chat(client)
    await send(client, identity)
    await finish(controller)

    failed = (await client.get(f"/api/chats/{identity}")).json()
    assert failed["status"] == "error"
    assert "five minutes" in failed["error"]
    assert "new chat" in failed["error"]
    assert failed["messages"][-1]["error"] == failed["error"]
