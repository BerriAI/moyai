"""Real Slack ingress keeps control and ownership fences ahead of input preparation."""

import pytest
import pytest_asyncio
import test_context_slack as context_tests
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import select

from moyai.models import Session, Turn
from moyai.product_slack_models import ProductSlackBinding, SlackTurnSource

bridge = context_tests.bridge
slack = context_tests.slack
ingress = context_tests.ingress


def followup(text: str, *, mentioned: bool = False):
    return context_tests.envelope({
        **context_tests.mention_message(),
        "type": "app_mention" if mentioned else "message",
        "ts": "1792255890.001",
        "text": ("<@UBOT00001> " if mentioned else "") + text,
        "files": [{"id": "FTEST0001"}],
    })


async def forbid_prepare(*args, **kwargs):
    pytest.fail("Controls and ineligible conversations must not prepare files or thread context.")


@pytest_asyncio.fixture
async def conversation(ingress, monkeypatch):
    async def request(method, url, **kwargs):
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(ingress.service.connectors, "request", request)
    await ingress.service.receive_payload(context_tests.envelope(context_tests.mention_message()))
    async with ingress.runtime.database.sessions() as db:
        binding = await db.scalar(select(ProductSlackBinding))
    return binding


async def test_top_level_mention_acknowledges_before_context_preparation(ingress, monkeypatch):
    service = ingress.service
    calls = []
    order = []

    async def request(method, url, **kwargs):
        if url == "https://slack.com/api/reactions.add":
            calls.append((method, url, kwargs))
            order.append("reaction")
            return {"ok": True}
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)

    message = context_tests.mention_message()
    message.pop("thread_ts")
    await service.receive_payload(context_tests.envelope(message))

    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST" and url == "https://slack.com/api/reactions.add"
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-bot-token"
    assert kwargs["json"] == {"channel": context_tests.CHANNEL, "name": "eyes", "timestamp": message["ts"]}
    assert order == ["reaction"]


async def test_bound_thread_followup_acknowledges_followup_timestamp(ingress, conversation, monkeypatch):
    service = ingress.service
    calls = []

    async def request(method, url, **kwargs):
        if url == "https://slack.com/api/reactions.add":
            calls.append((method, kwargs))
            return {"ok": True}
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(followup("An ordinary followup."))

    assert len(calls) == 1
    method, kwargs = calls[0]
    assert method == "POST"
    assert kwargs["json"] == {"channel": context_tests.CHANNEL, "name": "eyes", "timestamp": "1792255890.001"}


async def test_unaddressed_top_level_message_is_not_acknowledged(ingress, monkeypatch):
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(ingress.service.connectors, "request", request)
    message = {**context_tests.mention_message(), "type": "message", "text": "Unaddressed top-level message."}
    message.pop("thread_ts")

    await ingress.service.receive_payload(context_tests.envelope(message))

    assert "https://slack.com/api/reactions.add" not in calls


async def test_missing_scope_acknowledgement_does_not_block_turn_admission(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        if url == "https://slack.com/api/reactions.add":
            response = {"ok": False, "error": "missing_scope"}
            raise ConnectorError("The app rejected this operation.", code=response["error"], rejected=True)
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)

    await service.receive_payload(context_tests.envelope(context_tests.mention_message()))

    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(Turn)))) == 1


async def test_unexpected_attachment_read_error_is_logged_and_does_not_drop_turn(
    ingress, monkeypatch, caplog,
):
    service, runtime = ingress.service, ingress.runtime

    async def unexpected_error(*args):
        raise RuntimeError("synthetic attachment reader failure")

    async def request(method, url, **kwargs):
        if url == "https://slack.com/api/reactions.add":
            return {"ok": True}
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service.files.reader, "read", unexpected_error)
    monkeypatch.setattr(service.connectors, "request", request)
    await service.receive_payload(context_tests.envelope({
        **context_tests.mention_message(), "files": [{"id": "FTEST0001"}],
    }))
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)

    assert "file_unavailable" in prepared.prompt
    assert any(
        record.name == "moyai.product_slack_files"
        and record.getMessage() == "Slack attachment read unavailable (RuntimeError)"
        and not record.exc_info
        for record in caplog.records
    )
    assert "synthetic attachment reader failure" not in caplog.text
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(Turn)))) == 1


async def test_admission_persists_slack_origin_once_for_duplicate_delivery(ingress, monkeypatch):
    service, runtime = ingress.service, ingress.runtime

    async def request(method, url, **kwargs):
        if url == "https://slack.com/api/reactions.add":
            return {"ok": True}
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service.connectors, "request", request)
    payload = context_tests.envelope(context_tests.mention_message())
    await service.receive_payload(payload)
    await service.receive_payload(payload)

    async with runtime.database.sessions() as db:
        source_rows = list(await db.scalars(select(SlackTurnSource)))
        assert len(source_rows) == 1
        assert source_rows[0].message_ts == context_tests.MENTION_TS


@pytest.mark.parametrize("command,expected", [
    ("status", "This thread is listening."),
    ("stop", "Requested a stop"),
    ("sleep", "Paused this thread"),
    ("wake", "Listening again"),
    ("model test", "New messages in this session will use test"),
    ("answer My answer", "This session is not waiting for an answer."),
    ("continue", "This session is not waiting for an answer."),
])
async def test_controls_bypass_input_readers(ingress, conversation, monkeypatch, command, expected):
    service, runtime = ingress.service, ingress.runtime
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)

    await service.receive_payload(followup("/" + command, mentioned=True))

    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn)))
        binding = await db.get(ProductSlackBinding, conversation.session_id)
        session = await db.get(Session, conversation.session_id)
    controls = [turn for turn in turns if runtime.security.unseal(turn.prompt).decode() == "Slack control"]
    assert len(turns) == 2 and len(controls) == 1
    assert controls[0].status == "completed"
    assert expected in runtime.security.unseal(controls[0].result).decode()
    assert binding.paused is (command == "sleep")
    assert session.model == "test"


@pytest.mark.parametrize("command,allowed", [
    ("status", True), ("wake", True),
    ("sleep", False), ("stop", False), ("model test", False),
    ("answer My answer", False), ("continue", False),
])
async def test_paused_thread_accepts_only_unmentioned_status_and_wake(
    ingress, conversation, monkeypatch, command, allowed,
):
    service, runtime = ingress.service, ingress.runtime
    await service.set_paused(conversation.session_id, True)
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)

    await service.receive_payload(followup("/" + command))

    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn)))
        binding = await db.get(ProductSlackBinding, conversation.session_id)
    assert len(turns) == 1 + int(allowed)
    assert binding.paused is (command != "wake")
    if allowed:
        control = next(turn for turn in turns if turn.request_key.startswith("command:"))
        assert control.status == "completed"
        expected = "This thread is paused." if command == "status" else "Listening again"
        assert expected in runtime.security.unseal(control.result).decode()


async def test_pause_during_file_read_prevents_followup_admission(ingress, conversation, monkeypatch):
    service, runtime = ingress.service, ingress.runtime
    reads = []

    async def pause_during_read(*args):
        reads.append("files")
        await service.set_paused(conversation.session_id, True)
        return {"uploads": [], "images": [], "context": ""}

    monkeypatch.setattr(service.files, "prepare", pause_during_read)
    await service.receive_payload(followup("An ordinary followup with a file."))

    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn)))
        binding = await db.get(ProductSlackBinding, conversation.session_id)
    assert reads == ["files"]
    assert len(turns) == 1 and binding.paused


@pytest.mark.parametrize("field", ["private_owner_id", "deleted_at"])
async def test_historical_privacy_fence_precedes_input_readers(ingress, monkeypatch, field):
    service, runtime = ingress.service, ingress.runtime
    source = service.store.create_run(
        "Historical thread", "", "demo", [], chat_enabled=True, user_id="google:owner",
        private_owner_id="google:owner" if field == "private_owner_id" else "",
    )
    service.store.execute(
        "INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)",
        (context_tests.TEAM, context_tests.CHANNEL, context_tests.ROOT_TS, source["id"], context_tests.ROOT_TS),
    )
    if field == "deleted_at":
        service.store.execute("UPDATE runs SET deleted_at=? WHERE id=?", ("2026-10-17", source["id"]))
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)

    with pytest.raises(HTTPException, match="private or deleted") as error:
        await service.receive_payload(followup("Review this thread.", mentioned=True))
    assert error.value.status_code == 403
    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []


@pytest.mark.parametrize("quote", [
    "`<@UBOT00001>`", "```\n<@UBOT00001>\n```", "> <@UBOT00001>",
    ">>>\n<@UBOT00001>", "`<@UBOT00001>", "```\n<@UBOT00001>",
])
async def test_quoted_mentions_do_not_start_a_conversation(ingress, monkeypatch, quote):
    service, runtime = ingress.service, ingress.runtime
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)

    await service.receive_payload(followup("A literal example:\n" + quote))

    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []


async def test_addressed_message_preserves_mentions_inside_quotes(ingress, conversation):
    service, runtime = ingress.service, ingress.runtime
    question = "Explain this literal example: `<@UBOT00001> status`"
    payload = followup(question, mentioned=True)
    del payload["event"]["files"]

    await service.receive_payload(payload)

    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn)))
    assert len(turns) == 2
    assert any(runtime.security.unseal(turn.prompt).decode().startswith(question) for turn in turns)


@pytest.mark.parametrize("field,value", [
    ("ts", "not-a-timestamp"), ("thread_ts", "1792259999.001"),
    ("channel", "C!INVALID"), ("text", {"not": "a string"}),
])
async def test_malformed_message_is_rejected_before_input_readers(ingress, monkeypatch, field, value):
    service, runtime = ingress.service, ingress.runtime
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)
    payload = followup("Review this thread.", mentioned=True)
    payload["event"][field] = value

    with pytest.raises(HTTPException) as error:
        await service.receive_payload(payload)
    assert error.value.status_code == 400
    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []


@pytest.mark.parametrize("text", ["!aside Keep this out of context.", "(aside) A human discussion.", "!ASIDE Private note."])
async def test_addressed_asides_do_not_start_a_conversation(ingress, monkeypatch, text):
    service, runtime = ingress.service, ingress.runtime
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)

    await service.receive_payload(followup(text, mentioned=True))

    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []


@pytest.mark.parametrize("channel_type", [None, "channel", "mpim"])
async def test_dm_requires_explicit_direct_channel_type(ingress, monkeypatch, channel_type):
    service, runtime = ingress.service, ingress.runtime
    monkeypatch.setattr(service.files, "prepare", forbid_prepare)
    payload = followup("Read this attachment.")
    payload["event"]["channel"] = "DTEST0001"
    if channel_type is not None:
        payload["event"]["channel_type"] = channel_type

    await service.receive_payload(payload)

    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []


async def test_signed_edits_update_queued_input_but_never_rerun_started_work(ingress, conversation, monkeypatch):
    from moyai.models import Message, MessageSource
    service, runtime = ingress.service, ingress.runtime
    async with runtime.database.sessions() as db:
        turn = await db.scalar(select(Turn))
        count_before = len(list(await db.scalars(select(Turn))))
    original = context_tests.mention_message()
    original['channel'] = context_tests.CHANNEL
    changed = {**original, 'text': '<@UBOT00001> updated task', 'edited': {'ts': '1792255900.001'}}
    payload = context_tests.envelope(original)
    payload['event'] = {'type': 'message', 'subtype': 'message_changed', 'channel': context_tests.CHANNEL,
                        'previous_message': original, 'message': changed, 'event_ts': '1792255900.001'}
    assert (await service.receive_payload(payload))['updated']
    async with runtime.database.sessions() as db:
        fresh = await db.get(Turn, turn.id)
        assert runtime.security.unseal(fresh.prompt).decode().startswith('updated task')
        assert len(list(await db.scalars(select(Turn)))) == count_before
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    changed['text'] = '<@UBOT00001> second edit after starting'
    changed['edited']['ts'] = '1792255910.001'
    assert (await service.receive_payload(payload))['updated']
    async with runtime.database.sessions() as db:
        fresh = await db.get(Turn, turn.id)
        assert runtime.security.unseal(fresh.prompt).decode() == prepared.prompt
        message = await db.scalar(select(Message).where(Message.turn_id == turn.id))
        assert runtime.security.unseal(message.content).decode() == 'second edit after starting'
        source = await db.get(MessageSource, message.identity)
        assert not source.mirror
    deleted = {**payload, 'event': {'type': 'message', 'subtype': 'message_deleted',
        'channel': context_tests.CHANNEL, 'previous_message': changed,
        'deleted_ts': original['ts'], 'event_ts': '1792255920.001'}}
    assert (await service.receive_payload(deleted))['updated']
    messages = await runtime.database.messages(await runtime.database.user(turn.actor_id), turn.session_id)
    assert messages[0]['content'] == '' and messages[0]['deleted']
    assert messages[0]['edited_after_start']
    assert not (await service.receive_payload(payload))['updated']  # late edit cannot resurrect a deletion
