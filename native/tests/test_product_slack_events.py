"""Real Slack ingress keeps control and ownership fences ahead of input preparation."""

import pytest
import pytest_asyncio
import test_product_slack_context as context_tests
from fastapi import HTTPException
from sqlalchemy import select

from moyai.models import Session, Turn
from moyai.product_slack_models import ProductSlackBinding

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
    monkeypatch.setattr(service.context, "prepare", forbid_prepare)

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
    monkeypatch.setattr(service.context, "prepare", forbid_prepare)

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
    monkeypatch.setattr(service.context, "prepare", forbid_prepare)

    with pytest.raises(HTTPException, match="private or deleted") as error:
        await service.receive_payload(followup("Review this thread.", mentioned=True))
    assert error.value.status_code == 403
    async with runtime.database.sessions() as db:
        assert list(await db.scalars(select(Turn))) == []
