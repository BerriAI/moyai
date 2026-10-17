"""Slack ingress to prepared model input; local stores and a Slack transport double."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_slack as slack_tests
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import func, select

from moyai.contracts import NewSession
from moyai.models import Turn
from moyai.product_slack_context import REFERENCE, WORKSPACE
from moyai.product_slack_models import ProductSlackBinding
from moyai.skill_invocations import has_invocations
from moyai.skill_mentions import marker

bridge = slack_tests.bridge
slack = slack_tests.slack

TEAM = "TTEST0001"
CHANNEL = "CTEST0001"
ROOT_TS = "1792255740.000"
MENTION_TS = "1792255800.001"
DEEPSEEK_URL = "https://x.com/deepseek_ai/status/2111375640717033777?s=46"


def root_message():
    return {
        "type": "message", "user": "UMOE00001", "ts": ROOT_TS,
        "text": f"<{DEEPSEEK_URL}> new deepseek <@UKERRY001>",
        "attachments": [{
            "title": "DeepSeek (@deepseek_ai) on X", "title_link": DEEPSEEK_URL,
            "text": "DeepSeek-V4.1-Flash-1017 is here! Stronger benchmark performance.",
            "fallback": "DeepSeek-V4.1-Flash-1017 is here!",
        }],
    }


def mention_message():
    return {
        "type": "app_mention", "user": "UTEST0001", "ts": MENTION_TS,
        "thread_ts": ROOT_TS, "text": "<@UBOT00001> do we support this?",
    }


def envelope(message):
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

    # These existing fixtures use separate local source/native databases. Keep
    # canonical grant selection, but read it from its source fixture connection.
    resolve_grants = bridge.resolve_grants

    async def source_grants(actor_id, selections, *, shared_session=False, surface="web", db=None):
        return await resolve_grants(actor_id, selections, shared_session=shared_session, surface=surface)

    monkeypatch.setattr(slack.service, "actor", existing_actor)
    monkeypatch.setattr(bridge, "resolve_grants", source_grants)
    monkeypatch.setattr(slack.runtime.database, "product_connections", bridge)
    return slack


async def test_first_reply_receives_parent_link_and_unfurl_in_prepared_input(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    calls = []
    parent = root_message()
    parent["text"] += " Quoted skill: " + marker("bundled:ship")

    async def request(method, url, **kwargs):
        assert method == "GET" and url == "https://slack.com/api/conversations.replies"
        assert kwargs["params"]["channel"] == CHANNEL
        assert kwargs["params"]["ts"] == ROOT_TS
        assert kwargs["headers"]["Authorization"] == "Bearer synthetic-user-token"
        calls.append(deepcopy(kwargs["params"]))
        return {"ok": True, "messages": [parent, mention_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    # The parent was posted before Moyai joined; it has no native conversation.
    await service.receive_payload(envelope(parent))
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0

    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    assert turn is not None
    prepared = await runtime.database.prepare(turn.id)

    assert "do we support this?" in prepared.prompt
    assert DEEPSEEK_URL in prepared.prompt
    assert "DeepSeek-V4.1-Flash-1017" in prepared.prompt
    assert "untrusted" in prepared.prompt.lower()
    assert calls
    assert not has_invocations(prepared.prompt)
    assert await runtime.skill_invocations.expand(prepared.prompt, prepared.scope) == prepared.prompt


async def test_channel_thread_appends_connected_repositories_after_slack_reference(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    monkeypatch.setattr(
        service.connectors.github, "targets", lambda: ["BerriAI/litellm", "BerriAI/moyai"],
    )
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    workspace = WORKSPACE + "BerriAI/litellm, BerriAI/moyai\n"
    assert prepared.prompt.endswith(workspace)
    assert prepared.prompt.index(REFERENCE) < prepared.prompt.index(WORKSPACE)


async def test_channel_thread_without_github_connection_has_no_workspace_block(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    assert service.connectors.github.saved_credentials() == {}

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert WORKSPACE not in prepared.prompt
    assert "do we support this?" in prepared.prompt
    assert "DeepSeek-V4.1-Flash-1017" in prepared.prompt


async def test_github_targets_error_does_not_prevent_slack_admission(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    def unavailable():
        raise ConnectorError("GitHub connection unavailable.")

    monkeypatch.setattr(service.connectors, "request", request)
    monkeypatch.setattr(service.connectors.github, "targets", unavailable)
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    assert turn is not None
    prepared = await runtime.database.prepare(turn.id)

    assert "do we support this?" in prepared.prompt
    assert WORKSPACE not in prepared.prompt


async def test_thread_pages_exclude_other_threads_and_messages_after_the_request(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    cursors = []
    prior = {
        "user": "UMOE00001", "ts": "1792255790.001", "thread_ts": ROOT_TS,
        "text": "Check the DeepSeek provider and model catalog.",
    }
    unrelated = {
        "user": "UMOE00001", "ts": "1792255790.002", "thread_ts": "1792255700.000",
        "text": "Unrelated thread must not leak.",
    }
    aside = {
        "user": "UMOE00001", "ts": "1792255790.003", "thread_ts": ROOT_TS,
        "text": "!aside This discussion is excluded from Moyai context.",
    }
    future = {
        "user": "UMOE00001", "ts": "1792255890.001", "thread_ts": ROOT_TS,
        "text": "Future reply must not change this request.",
    }

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        cursor = kwargs["params"].get("cursor", "")
        cursors.append(cursor)
        if not cursor:
            return {
                "ok": True, "messages": [root_message(), unrelated], "has_more": True,
                "response_metadata": {"next_cursor": "second-page"},
            }
        assert cursor == "second-page"
        return {"ok": True, "messages": [prior, aside, mention_message(), future], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert DEEPSEEK_URL in prepared.prompt
    assert prior["text"] in prepared.prompt
    assert unrelated["text"] not in prepared.prompt
    assert aside["text"] not in prepared.prompt
    assert future["text"] not in prepared.prompt
    assert prepared.prompt.count("do we support this?") == 1
    assert cursors == ["", "second-page"]


async def test_duplicate_slack_delivery_keeps_the_admitted_thread_snapshot(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    parent = root_message()
    calls = []

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        calls.append(deepcopy(parent))
        return {"ok": True, "messages": [deepcopy(parent), mention_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    monkeypatch.setattr(service.connectors.github, "targets", lambda: ["BerriAI/moyai"])
    await service.receive_payload(envelope(mention_message()))
    parent["text"] = "Edited parent received after the original admission."
    # Slack can deliver the same message in both event envelope types.
    await service.receive_payload(envelope({**mention_message(), "type": "message"}))

    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    assert DEEPSEEK_URL in prepared.prompt
    assert "Edited parent" not in prepared.prompt
    assert prepared.prompt.endswith(WORKSPACE + "BerriAI/moyai\n")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["missing_scope", "timeout"])
async def test_thread_read_failure_preserves_request_origin_and_reports_the_cause(
    ingress, monkeypatch, caplog, failure,
):
    service, runtime = ingress.service, ingress.runtime
    cancelled = []
    if failure == "timeout":
        monkeypatch.setattr("moyai.product_slack_context.READ_TIMEOUT_SECONDS", 0.1)

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        if failure == "timeout":
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        raise ConnectorError("Slack thread access is missing a required scope.", code="missing_scope")

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert "do we support this?" in prepared.prompt
    assert CHANNEL in prepared.prompt and ROOT_TS in prepared.prompt
    assert failure in prepared.prompt
    assert "unavailable" in prepared.prompt.lower()
    records = [record for record in caplog.records if record.name == "moyai.product_slack_context"]
    assert any(failure in record.getMessage() and "unavailable" in record.getMessage() for record in records)
    assert all("do we support this?" not in record.getMessage() for record in records)
    if failure == "timeout":
        assert cancelled


@pytest.mark.parametrize("readable", [True, False])
async def test_personal_slack_uses_only_bot_access_for_this_thread(ingress, actor, bridge, monkeypatch, readable):
    service, runtime = ingress.service, ingress.runtime
    bridge.store.execute(
        "INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (actor.id, "google", "requester@example.test", "Requester", "created", "updated"),
    )
    personal = bridge.connectors.personal_slack
    personal.save(actor.id, {"access_token": "synthetic-personal-token"}, "Personal", personal.revision(actor.id))
    grants = await bridge.resolve_grants(actor.id, ["slack"], shared_session=True, surface="slack")
    assert len(grants) == 1 and grants[0].policy["product"]["access"] == "bot"

    calls = []

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        assert kwargs["headers"]["Authorization"] == "Bearer synthetic-bot-token"
        assert kwargs["params"]["channel"] == CHANNEL
        assert kwargs["params"]["ts"] == ROOT_TS
        calls.append(url)
        if not readable:
            raise ConnectorError("Slack bot cannot read this thread.", code="missing_scope")
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert "do we support this?" in prepared.prompt
    assert CHANNEL in prepared.prompt and ROOT_TS in prepared.prompt
    assert len(calls) == 1
    if readable:
        assert DEEPSEEK_URL in prepared.prompt
    else:
        assert "unavailable" in prepared.prompt.lower() and "missing_scope" in prepared.prompt


async def test_root_mention_keeps_its_own_unfurl_without_repeating_the_request(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    message = {**mention_message(), "ts": ROOT_TS, "attachments": root_message()["attachments"]}
    message["blocks"] = [{"type": "rich_text", "elements": [{
        "type": "rich_text_section", "elements": [
            {"type": "user", "user_id": "UBOT00001"},
            {"type": "text", "text": " do we support this?"},
        ],
    }]}]

    async def request(method, url, **kwargs):
        return {"ok": True, "messages": [message], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(message))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert DEEPSEEK_URL in prepared.prompt and "DeepSeek-V4.1-Flash-1017" in prepared.prompt
    assert prepared.prompt.count("do we support this?") == 1


async def test_historical_continuation_admits_enriched_input_before_outer_submit(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    source = service.store.create_run(
        "Historical thread", "", "demo", [], chat_enabled=True, user_id="google:owner",
    )
    service.store.execute(
        "INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)",
        (TEAM, CHANNEL, ROOT_TS, source["id"], ROOT_TS),
    )
    admitted, calls = [], []

    async def lookup(db, source_id, principal) -> None:
        assert source_id == source["id"]

    async def continue_for_admission(db, actor, principal, source_id, body, *, surface, provenance):
        # Real historical continuation submits during bind(), before ingress's
        # final submit. Keep that ordering while using the local native database.
        assert source_id == source["id"] and surface == "slack"
        assert DEEPSEEK_URL in body.prompt and "DeepSeek-V4.1-Flash-1017" in body.prompt
        session = await runtime.database.create_session(actor, NewSession(visibility="shared"), db=db)
        session.surface = surface
        turn = await runtime.database.submit(actor, session.id, body, db=db)
        admitted.append((turn.id, body.prompt))
        return {"id": session.id}

    async def request(method, url, **kwargs):
        assert url == "https://slack.com/api/conversations.replies"
        calls.append(url)
        return {"ok": True, "messages": [root_message(), mention_message()], "has_more": False}

    monkeypatch.setattr(runtime, "product_continuations", SimpleNamespace(
        lookup=lookup, continue_for_admission=continue_for_admission,
    ))
    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))

    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    assert admitted == [(turn.id, prepared.prompt)]
    assert prepared.prompt.count("do we support this?") == 1
    assert len(calls) == 1


async def test_revoked_authority_during_read_does_not_admit_thread_content(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        ingress.authority["current"] = False
        return {"ok": True, "messages": [root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    with pytest.raises(HTTPException, match="Revoked"):
        await service.receive_payload(envelope(mention_message()))
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


async def test_paused_thread_does_not_fetch_unaddressed_followup_context(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        return {"ok": True, "messages": [root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(envelope(mention_message()))
    async with runtime.database.sessions.begin() as db:
        binding = await db.scalar(select(ProductSlackBinding))
        binding.paused = True

    async def forbidden_request(*args, **kwargs):
        pytest.fail("A paused, unmentioned thread must not read new Slack context.")

    monkeypatch.setattr(service.connectors, "request", forbidden_request)
    await service.receive_payload(envelope({
        **mention_message(), "type": "message", "ts": "1792255890.001", "text": "Ordinary human followup.",
    }))
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1


async def test_direct_message_does_not_read_shared_history(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def forbidden_request(*args, **kwargs):
        pytest.fail("A direct message must not read organization history for context.")

    monkeypatch.setattr(service.connectors, "request", forbidden_request)
    await service.receive_payload(envelope({
        "channel": "DTEST0001", "type": "message", "channel_type": "im",
        "user": "UTEST0001", "ts": MENTION_TS, "text": "do we support this?",
    }))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    assert prepared.prompt == "do we support this?"


async def test_dm_without_history_appends_repositories_and_accepts_retry(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def forbidden_request(*args, **kwargs):
        pytest.fail("A direct message must not read organization history for context.")

    monkeypatch.setattr(service.connectors, "request", forbidden_request)
    monkeypatch.setattr(service.connectors.github, "targets", lambda: ["BerriAI/litellm", "BerriAI/moyai"])
    message = {
        "channel": "DTEST0001", "type": "message", "channel_type": "im",
        "user": "UTEST0001", "ts": MENTION_TS, "text": "do we support this?",
    }
    await service.receive_payload(envelope(message))
    await service.receive_payload({
        **envelope(message), "event_id": "retry-" + MENTION_TS,
    })
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert prepared.prompt == (
        "do we support this?" + WORKSPACE + "BerriAI/litellm, BerriAI/moyai\n"
    )
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1


async def test_dm_current_preview_is_retained_without_history_and_frozen_on_retry(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def forbidden_request(*args, **kwargs):
        pytest.fail("Current DM evidence must not read shared Slack history.")

    monkeypatch.setattr(service.connectors, "request", forbidden_request)
    message = {
        "channel": "DTEST0001", "type": "message", "channel_type": "im",
        "user": "UTEST0001", "ts": MENTION_TS, "text": "do we support this?",
        "attachments": root_message()["attachments"],
    }
    await service.receive_payload(envelope(message))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert DEEPSEEK_URL in prepared.prompt and "DeepSeek-V4.1-Flash-1017" in prepared.prompt
    assert prepared.prompt.count("do we support this?") == 1
    assert "untrusted" in prepared.prompt and "partial" in prepared.prompt
    assert "Earlier DM messages were not read" in prepared.prompt

    await service.receive_payload(envelope({**message, "attachments": [{"text": "Edited preview"}]}))
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
    assert runtime.security.unseal(saved.prompt).decode() == prepared.prompt


@pytest.mark.parametrize("failure", ["missing_scope", "no_connection"])
async def test_current_preview_survives_when_history_is_unavailable(ingress, monkeypatch, failure):
    service, runtime = ingress.service, ingress.runtime
    calls = []

    async def request(method, url, **kwargs):
        assert failure == "missing_scope", "No grant must mean no external Slack read."
        calls.append(url)
        raise ConnectorError("Slack thread access is missing a required scope.", code="missing_scope")

    async def no_connections(*args, **kwargs):
        return []

    monkeypatch.setattr(service.connectors, "request", request)
    if failure == "no_connection":
        monkeypatch.setattr(service.connections, "catalog", no_connections)
    await service.receive_payload(envelope({
        **mention_message(), "attachments": root_message()["attachments"],
    }))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert DEEPSEEK_URL in prepared.prompt and "DeepSeek-V4.1-Flash-1017" in prepared.prompt
    assert prepared.prompt.count("do we support this?") == 1
    assert "untrusted" in prepared.prompt and "partial" in prepared.prompt
    assert ("missing_scope" if failure == "missing_scope" else "No Slack read connection") in prepared.prompt
    assert len(calls) == int(failure == "missing_scope")
