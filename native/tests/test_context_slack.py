"""Durable Slack thread context source and its production reader."""

from __future__ import annotations

import pytest
import pytest_asyncio
import test_product_slack as slack_tests
from app.connector_errors import ConnectorError
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from moyai.context.session import SessionContext
from moyai.context.slack import SlackThread, make_slack_thread_reader
from moyai.contracts import RunScope

bridge = slack_tests.bridge
slack = slack_tests.slack

TEAM = "TTEST0001"
CHANNEL = "CTEST0001"
ROOT_TS = "1792255740.000"
MENTION_TS = "1792255800.001"
DEEPSEEK_URL = "https://x.com/deepseek_ai/status/2111375640717033777?s=46"
ACK_URL = "https://slack.com/api/reactions.add"


def root_message() -> dict:
    return {
        "type": "message", "user": "UMOE00001", "ts": ROOT_TS,
        "text": f"<{DEEPSEEK_URL}> new deepseek <@UKERRY001>",
        "attachments": [{
            "title": "DeepSeek (@deepseek_ai) on X", "title_link": DEEPSEEK_URL,
            "text": "DeepSeek-V4.1-Flash-1017 is here! Stronger benchmark performance.",
            "fallback": "DeepSeek-V4.1-Flash-1017 is here!",
        }],
    }


def mention_message() -> dict:
    return {
        "type": "app_mention", "user": "UTEST0001", "ts": MENTION_TS,
        "thread_ts": ROOT_TS, "text": "<@UBOT00001> do we support this?",
    }


def envelope(message: dict) -> dict:
    return {
        "type": "event_callback", "team_id": TEAM,
        "event_id": "event-" + message["ts"] + "-" + message["type"],
        "event": {"channel": CHANNEL, **message},
    }


@pytest_asyncio.fixture
async def ingress(slack, actor, bridge, monkeypatch):
    principal = await slack.service.product.identity.require_current(slack.grant)

    async def existing_actor(team, user):
        assert (team, user) == (TEAM, "UTEST0001")
        return actor, principal

    resolve_grants = bridge.resolve_grants

    async def source_grants(actor_id, selections, *, shared_session=False, surface="web", db=None):
        return await resolve_grants(actor_id, selections, shared_session=shared_session, surface=surface)

    monkeypatch.setattr(slack.service, "actor", existing_actor)
    monkeypatch.setattr(bridge, "resolve_grants", source_grants)
    monkeypatch.setattr(slack.runtime.database, "product_connections", bridge)
    return slack


async def render_source(ingress, turn_id: str) -> str:
    prepared = await ingress.runtime.database.prepare(turn_id)
    return await SlackThread(make_slack_thread_reader(ingress.service)).render(prepared.scope)


async def admit_mention(ingress, monkeypatch, request):
    monkeypatch.setattr(ingress.service.connectors, "request", request)
    await ingress.service.receive_payload(envelope(mention_message()))
    turn = await ingress.runtime.database.claim_next()
    assert turn is not None
    return turn


async def test_non_slack_surface_does_not_call_reader() -> None:
    calls = []

    async def reader(scope: RunScope) -> tuple[str, bool]:
        calls.append(scope)
        return "unexpected", True

    rendered = await SlackThread(reader).render(RunScope(
        session_id="session", turn_id="turn", actor_id="actor", model="test", surface="web",
    ))

    assert rendered == ""
    assert calls == []


@pytest.mark.parametrize(
    ("cacheable", "expected_calls"),
    [(True, 1), (False, 2)],
    ids=["complete", "unavailable"],
)
async def test_slack_render_caches_only_cacheable_context(cacheable, expected_calls):
    calls = []

    async def reader(scope: RunScope) -> tuple[str, bool]:
        calls.append(scope.turn_id)
        return "context", cacheable

    source = SlackThread(reader)
    scope = RunScope(
        session_id="session", turn_id="turn", actor_id="actor", model="test", surface="slack",
    )

    assert await source.render(scope) == "context"
    assert await source.render(scope) == "context"
    assert calls == ["turn"] * expected_calls


async def test_slack_render_cache_evicts_oldest_entry():
    calls = []

    async def reader(scope: RunScope) -> tuple[str, bool]:
        calls.append(scope.turn_id)
        return scope.turn_id, True

    source = SlackThread(reader)
    for turn_id in range(256):
        await source.render(RunScope(
            session_id="session", turn_id=str(turn_id), actor_id="actor", model="test", surface="slack",
        ))
    await source.render(RunScope(
        session_id="session", turn_id="256", actor_id="actor", model="test", surface="slack",
    ))
    await source.render(RunScope(
        session_id="session", turn_id="0", actor_id="actor", model="test", surface="slack",
    ))

    assert len(calls) == 258
    assert calls[-1] == "0"


async def test_shared_channel_reads_with_bot_without_requester_grant(ingress, monkeypatch):
    calls = []

    async def no_connections(*args, **kwargs):
        return []

    async def request(method, url, **kwargs):
        if url == ACK_URL:
            return {"ok": True}
        calls.append((kwargs["headers"]["Authorization"], kwargs["params"]["channel"], kwargs["params"]["ts"]))
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    monkeypatch.setattr(ingress.service.connections, "catalog", no_connections)
    turn = await admit_mention(ingress, monkeypatch, request)
    rendered = await render_source(ingress, turn.id)

    assert DEEPSEEK_URL in rendered
    assert "DeepSeek-V4.1-Flash-1017" in rendered
    assert '"status": "complete"' in rendered
    assert "do we support this?" not in rendered
    assert calls == [("Bearer synthetic-bot-token", CHANNEL, ROOT_TS)]


async def test_bot_read_uses_bot_when_user_token_cannot_join_thread(ingress, monkeypatch):
    calls = []

    async def request(method, url, **kwargs):
        if url == ACK_URL:
            return {"ok": True}
        authorization = kwargs["headers"]["Authorization"]
        calls.append(authorization)
        if authorization != "Bearer synthetic-bot-token":
            raise ConnectorError("The user token is not a member of this channel.", code="not_in_channel")
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    turn = await admit_mention(ingress, monkeypatch, request)
    rendered = await render_source(ingress, turn.id)

    assert DEEPSEEK_URL in rendered
    assert calls == ["Bearer synthetic-bot-token"]


async def test_bot_missing_scope_without_grant_is_unavailable(ingress, monkeypatch):
    calls = []

    async def no_connections(*args, **kwargs):
        return []

    async def request(method, url, **kwargs):
        if url == ACK_URL:
            return {"ok": True}
        calls.append(kwargs["headers"]["Authorization"])
        raise ConnectorError("Slack thread access is missing a required scope.", code="missing_scope")

    monkeypatch.setattr(ingress.service.connections, "catalog", no_connections)
    turn = await admit_mention(ingress, monkeypatch, request)
    rendered = await render_source(ingress, turn.id)

    assert "No Slack read connection is available" in rendered
    assert "missing_scope" in rendered
    assert calls == ["Bearer synthetic-bot-token"]


async def test_triggering_message_is_not_duplicated(ingress, monkeypatch):
    async def request(method, url, **kwargs):
        if url == ACK_URL:
            return {"ok": True}
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    turn = await admit_mention(ingress, monkeypatch, request)
    rendered = await render_source(ingress, turn.id)

    assert "do we support this?" not in rendered
    assert DEEPSEEK_URL in rendered


async def test_dm_and_missing_rows_render_empty(ingress, monkeypatch):
    async def forbidden_request(*args, **kwargs):
        if args[1] == ACK_URL:
            return {"ok": True}
        pytest.fail("DM context must not read Slack history.")

    monkeypatch.setattr(ingress.service.connectors, "request", forbidden_request)
    message = {
        "channel": "DTEST0001", "type": "message", "channel_type": "im",
        "user": "UTEST0001", "ts": MENTION_TS, "text": "do we support this?",
    }
    await ingress.service.receive_payload(envelope(message))
    turn = await ingress.runtime.database.claim_next()
    assert turn is not None
    assert await render_source(ingress, turn.id) == ""
    assert await SlackThread(make_slack_thread_reader(ingress.service)).render(RunScope(
        session_id="missing", turn_id="missing", actor_id="actor", model="test", surface="slack",
    )) == ""


async def test_slack_source_appears_in_session_context() -> None:
    async def reader(scope: RunScope) -> tuple[str, bool]:
        return f"Slack context for {scope.turn_id}", True

    context = SessionContext([SlackThread(reader)])
    rendered = await context.render_sources(RunContext(
        deps=RunScope(
            session_id="session", turn_id="turn", actor_id="actor", model="test", surface="slack",
        ),
        model=TestModel(),
        usage=RunUsage(),
    ))

    assert rendered == "Slack context for turn"
