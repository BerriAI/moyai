"""Synthetic Slack stays local throughout a Harbor turn; other HTTP stays real."""
from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("harbor_slack_fixture_test", ROOT / "evals/moyai_harbor/slack_fixture.py")
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.mark.asyncio
async def test_exact_slack_host_is_intercepted_and_other_transports_preserved():
    slack_calls, other_calls, mounted_calls = [], [], []

    def slack(request):
        slack_calls.append(request)
        return httpx.Response(200, json={"ok": True})

    def other(request):
        other_calls.append(request)
        return httpx.Response(202, json={"gateway": "unchanged"})

    def mounted(request):
        mounted_calls.append(request)
        return httpx.Response(201)

    original_client = httpx.AsyncClient
    original_init = httpx.AsyncClient.__init__
    with fixture.route_slack_clients(httpx.MockTransport(slack)):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(other),
            mounts={"all://custom.example": httpx.MockTransport(mounted)},
            headers={"Authorization": "Bearer synthetic-slack-token"},
        ) as client:
            assert isinstance(client, original_client)
            assert httpx.AsyncClient is original_client
            assert (await client.get("https://slack.com/api/conversations.replies")).status_code == 200
            for host in ("gateway.example", "slack.com.example", "files.slack.com"):
                response = await client.post(f"https://{host}/v1/responses", json={"model": "real-model"})
                assert response.status_code == 202
                assert response.json() == {"gateway": "unchanged"}
            assert (await client.get("https://custom.example/")).status_code == 201
    assert httpx.AsyncClient is original_client
    assert httpx.AsyncClient.__init__ is original_init
    assert len(slack_calls) == 1
    assert slack_calls[0].headers["Authorization"] == "Bearer synthetic-slack-token"
    assert [request.url.host for request in other_calls] == ["gateway.example", "slack.com.example", "files.slack.com"]
    assert len(mounted_calls) == 1


def test_fixture_restores_client_constructor_after_workflow_failure():
    original_init = httpx.AsyncClient.__init__
    with pytest.raises(RuntimeError, match="workflow failed"), fixture.route_slack_clients(httpx.MockTransport(lambda _: httpx.Response(200))):
        raise RuntimeError("workflow failed")
    assert httpx.AsyncClient.__init__ is original_init


@pytest.mark.asyncio
async def test_delayed_workflow_context_read_uses_fixture_without_slack_network(monkeypatch):
    # The complete runner integration uses Harbor's optional development overlay.
    pytest.importorskip("harbor")
    pytest.importorskip("moto")
    for directory in (ROOT / "native/tests", ROOT / "native/src", ROOT / "evals"):
        monkeypatch.syspath_prepend(str(directory))
    from moyai_harbor import turn_runner as runner
    from openai import AsyncOpenAI

    assert Path(runner.__file__).resolve() == ROOT / "evals/moyai_harbor/turn_runner.py"
    network_calls = []

    async def network(_transport, request):
        # Catch all underlying HTTP, including environment proxy transports.
        assert request.url.host != "slack.com", "Synthetic token reached production Slack transport"
        network_calls.append(request)
        return httpx.Response(200, json={"id": "gateway-result", "object": "response", "output": []})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    monkeypatch.setattr(runner, "offered_tool_names", AsyncMock(return_value=[]))
    received = False
    completed = SimpleNamespace(prompt=b"Explain team", conversation=None, usage="{}")
    scalar_values = iter([SimpleNamespace(session_id="session"), SimpleNamespace(id="turn", workflow_id="workflow")])
    db = SimpleNamespace(scalar=AsyncMock(side_effect=lambda *_: next(scalar_values)), get=AsyncMock(return_value=completed))

    @asynccontextmanager
    async def sessions():
        yield db

    async def receive(_payload):
        nonlocal received
        received = True

    async def workflow_result():
        await asyncio.sleep(0)
        assert received
        # This read happens after receive_payload, exactly where the old fixture
        # had already stopped intercepting requests.
        async with httpx.AsyncClient(headers={"Authorization": "Bearer synthetic-slack-bot-token"}) as client:
            response = await client.get("https://slack.com/api/conversations.replies", params={"channel": "CTEST0001"})
            assert response.json()["messages"][0]["text"] == "Earlier context"
            unknown = await client.get("https://slack.com/api/unconfigured.method")
            assert unknown.status_code == 404
            gateway_client = AsyncOpenAI(
                base_url="https://gateway.example/v1", api_key="synthetic-gateway-key",
                http_client=client,
            )
            gateway = await gateway_client.responses.create(model="real-model", input="Test input")
            assert gateway.id == "gateway-result"

    async def pump(client):
        await client.post("chat.postMessage", json={"channel": "CTEST0001", "blocks": [
            {"type": "section", "text": {"text": "A concrete answer"}},
        ]})

    runtime = SimpleNamespace(
        product_slack=SimpleNamespace(receive_payload=receive, pump=pump), product_connections=object(),
        database=SimpleNamespace(sessions=sessions, claim_next=AsyncMock()),
        settings=SimpleNamespace(temporal_queue="test", default_model="model"),
        security=SimpleNamespace(unseal=lambda value: value),
    )
    temporal = SimpleNamespace(client=SimpleNamespace(start_workflow=AsyncMock(
        return_value=SimpleNamespace(result=workflow_result),
    )))
    case = {"id": "style", "prompt": "Explain team", "thread": [
        {"role": "human", "display_name": "Reviewer", "text": "Earlier context"},
    ]}
    result = await runner.execute_turn(case, trial=1, thread=case["thread"], runtime=runtime, temporal=temporal, workspace=None)
    assert result["posted_reply"] == "A concrete answer"
    assert len(result["slack_thread_reads"]) == 1
    assert [request.url.host for request in network_calls] == ["gateway.example"]
