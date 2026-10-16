"""Recovery invariants; actual model/Modal proof is recorded separately."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import ApplicationError

from moyai.activities import save_computer
from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.models import ComputerDesktop, ComputerLease, Effect, Session, Turn, User
from moyai.security import Security
from moyai.settings import Settings
from moyai.workspace_recovery import checkpoint, prepare_workspace, save_checkpoint

REFERENCE = {"provider": "modal", "id": "sb-original"}
RESTORED = {"provider": "modal", "id": "sb-restored"}


async def saved(runtime, active, monkeypatch, *, waiting=True):
    monkeypatch.setattr("moyai.activities.current", lambda: runtime)
    session, turn, prepared = active
    conversation = ConversationTypeAdapter.dump_json(Conversation(
        conversation_id=prepared.scope.conversation_id,
    )).decode()
    assert await runtime.database.save_result(
        prepared.scope, conversation=conversation, output="" if waiting else "Saved.",
        workspace=REFERENCE, usage={"requests": 1}, deferred=waiting, checkpoint_workspace=True,
    )
    capture = AsyncMock(return_value=SimpleNamespace(object_id="im-checkpoint"))
    sandbox = SimpleNamespace(
        poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
        snapshot_filesystem=SimpleNamespace(aio=capture),
    )
    monkeypatch.setattr("moyai.workspace_recovery.modal.Sandbox.from_id.aio", AsyncMock(return_value=sandbox))
    return session, turn, prepared, capture


async def resumed(runtime, actor, active, monkeypatch, *, waiting=True):
    session, turn, _prepared, capture = await saved(runtime, active, monkeypatch, waiting=waiting)
    assert await save_checkpoint(runtime, turn.id, REFERENCE)
    if waiting:
        followup = await runtime.database.resume(
            actor, session.id, turn.id, "approved-once", {"approvals": {"original-call": True}},
        )
    else:
        followup = await runtime.database.submit(
            actor, session.id, NewMessage(prompt="Continue with the same files.", request_key="cold-followup"),
        )
    assert (await runtime.database.claim_next()).id == followup.id
    return session, turn, await runtime.database.prepare(followup.id), capture


async def test_pending_approval_is_not_exposed_until_checkpoint_settlement(runtime, actor, active, monkeypatch):
    session, turn, prepared, capture = await saved(runtime, active, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await runtime.database.resume(actor, session.id, turn.id, "too-early", {"approvals": {}})
    assert error.value.status_code == 409
    assert await save_checkpoint(runtime, turn.id, REFERENCE)
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        completed = await db.get(Turn, turn.id)
        point = checkpoint(current.snapshot)
        assert current.status == completed.status == "waiting"
        assert current.active_turn == turn.id
        assert completed.result_accepted
        assert point.image_id == "im-checkpoint"
        assert point.workspace == REFERENCE and point.scope == prepared.scope.workspace_key
        assert point.result_fingerprint == completed.result_fingerprint
    capture.assert_awaited_once_with(timeout=120)


@pytest.mark.parametrize("waiting", [True, False])
async def test_expired_resume_preserves_approval_and_effect_receipts(runtime, actor, active, monkeypatch, waiting):
    session, original, prepared, _ = await resumed(runtime, actor, active, monkeypatch, waiting=waiting)
    async with runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, prepared.scope.turn_id)
        results = turn.deferred_results
        source = await db.get(Turn, original.id)
        fingerprint, conversation = source.result_fingerprint, source.conversation
        db.add(Effect(
            id="confirmed-action", session_id=session.id, turn_id=original.id, actor_id=actor.id,
            tool_name="shell", arguments=runtime.security.seal("{}"), fingerprint="unchanged",
            state="confirmed",
        ))
    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", AsyncMock(return_value=False))
    create = AsyncMock(return_value=SimpleNamespace(
        object_id=RESTORED["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    ))
    monkeypatch.setattr("moyai.workspace_recovery.restored_sandbox", create)
    result = await prepare_workspace(runtime, prepared)
    assert result.workspace == RESTORED
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        turn = await db.get(Turn, prepared.scope.turn_id)
        source = await db.get(Turn, original.id)
        effect = await db.get(Effect, "confirmed-action")
        assert json.loads(current.workspace) == RESTORED and current.snapshot is None
        assert turn.deferred_results == results
        assert source.result_fingerprint == fingerprint and source.conversation == conversation
        assert effect.state == "confirmed" and effect.fingerprint == "unchanged"
    create.assert_awaited_once()
    # A lost activity acknowledgment loads the published reference; it does not
    # create another computer or roll it back to the prior savepoint.
    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", AsyncMock(return_value=True))
    replay = await runtime.database.prepare(prepared.scope.turn_id)
    assert (await prepare_workspace(runtime, replay)).workspace == RESTORED
    create.assert_awaited_once()


@pytest.mark.parametrize("mutation", ["missing", "raw_image", "different_scope", "different_conversation", "pending_effect"])
async def test_restore_refuses_unverified_or_uncertain_state(runtime, actor, active, monkeypatch, mutation):
    session, original, prepared, _ = await resumed(runtime, actor, active, monkeypatch)
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        if mutation == "missing":
            current.snapshot = None
        elif mutation == "raw_image":
            current.snapshot = "im-old-unbound-image"
        elif mutation == "different_scope":
            current.snapshot = checkpoint(current.snapshot).model_copy(update={"scope": "other"}).model_dump_json()
        elif mutation == "different_conversation":
            current.conversation = runtime.security.seal("{}")
        else:
            db.add(Effect(
                id="uncertain-action", session_id=session.id, turn_id=original.id, actor_id=actor.id,
                tool_name="shell", arguments=runtime.security.seal("{}"), fingerprint="unchanged", state="pending",
            ))
    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", AsyncMock(return_value=False))
    create = AsyncMock()
    monkeypatch.setattr("moyai.workspace_recovery.restored_sandbox", create)
    with pytest.raises(ApplicationError):
        await prepare_workspace(runtime, prepared)
    create.assert_not_awaited()
    async with runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, session.id)).workspace) == REFERENCE
        if mutation == "pending_effect":
            assert (await db.get(Effect, "uncertain-action")).state == "pending"


@pytest.mark.parametrize("change", ["revoke", "stop", "invalidate"])
async def test_late_restore_cannot_publish_after_authority_or_files_change(runtime, actor, active, monkeypatch, change):
    session, _, prepared, _ = await resumed(runtime, actor, active, monkeypatch)
    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", AsyncMock(return_value=False))

    async def create(*_):
        if change == "stop":
            await runtime.database.stop(actor, session.id)
        else:
            async with runtime.database.sessions.begin() as db:
                if change == "revoke":
                    (await db.get(User, actor.id)).enabled = False
                else:
                    (await db.get(Session, session.id)).snapshot = None
        return SimpleNamespace(object_id=RESTORED["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    monkeypatch.setattr("moyai.workspace_recovery.restored_sandbox", create)
    with pytest.raises((PermissionError, ApplicationError)):
        await prepare_workspace(runtime, prepared)
    async with runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, session.id)).workspace) == REFERENCE


async def test_failed_checkpoint_preserves_deferred_request(runtime, active, monkeypatch):
    session, turn, _, capture = await saved(runtime, active, monkeypatch)
    capture.side_effect = TimeoutError("Lost snapshot acknowledgment")
    await save_computer(turn.id, REFERENCE)
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        assert current.status == "waiting" and current.active_turn == turn.id
        assert (await db.get(Turn, turn.id)).status == "waiting"
        assert checkpoint(current.snapshot).image_id is None and current.save_warning
        assert await db.scalar(select(ComputerLease).where(ComputerLease.session_id == session.id)) is None


async def test_stop_during_deferred_checkpoint_does_not_rearm_approval(runtime, actor, active, monkeypatch):
    session, turn, _, capture = await saved(runtime, active, monkeypatch)

    async def stop(**_):
        await runtime.database.stop(actor, session.id)
        return SimpleNamespace(object_id="im-too-late")

    capture.side_effect = stop
    await save_computer(turn.id, REFERENCE)
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        assert current.status == "stopped" and current.active_turn is None
        assert (await db.get(Turn, turn.id)).status == "stopped"
        assert checkpoint(current.snapshot).image_id is None


async def test_exact_restore_inherits_legacy_file_provenance(runtime, actor, active, monkeypatch):
    from moyai.product_continuation_models import ProductWorkspaceRestoration

    async with runtime.database.engine.begin() as connection:
        await connection.run_sync(lambda conn: ProductWorkspaceRestoration.__table__.create(conn, checkfirst=True))
    session, _, prepared, _ = await resumed(runtime, actor, active, monkeypatch)
    async with runtime.database.sessions.begin() as db:
        db.add(ProductWorkspaceRestoration(
            session_id=session.id, workspace_id=REFERENCE["id"], workspace_scope=prepared.scope.workspace_key,
            manifest_fingerprint="verified-original-import", files=3, bytes=987,
        ))
    monkeypatch.setattr(runtime, "product", SimpleNamespace())
    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", AsyncMock(return_value=False))
    monkeypatch.setattr("moyai.workspace_recovery.restored_sandbox", AsyncMock(return_value=SimpleNamespace(
        object_id=RESTORED["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    )))
    await prepare_workspace(runtime, prepared)
    async with runtime.database.sessions() as db:
        restored = await db.get(ProductWorkspaceRestoration, (session.id, RESTORED["id"]))
        assert (restored.manifest_fingerprint, restored.files, restored.bytes) == ("verified-original-import", 3, 987)


async def test_read_only_desktop_binding_does_not_interrupt_restore(runtime, actor, active, monkeypatch):
    from moyai.computer import DESKTOP_VERSION
    from moyai.computer_stream import bind

    session, _, prepared, _ = await resumed(runtime, actor, active, monkeypatch)
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerDesktop(
            session_id=session.id, workspace_id=REFERENCE["id"], version=DESKTOP_VERSION,
            control_client="", control_epoch=str(uuid4()),
        ))

    async def unavailable(_reference):
        await bind(runtime, session.id, actor, str(uuid4()), controlling=False)
        return False

    monkeypatch.setattr("moyai.workspace_recovery.live_sandbox", unavailable)
    monkeypatch.setattr("moyai.workspace_recovery.restored_sandbox", AsyncMock(return_value=SimpleNamespace(
        object_id=RESTORED["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    )))
    assert (await prepare_workspace(runtime, prepared)).workspace == RESTORED


@pytest_asyncio.fixture
async def checkpoint_postgres(tmp_path):
    address = os.getenv("MOYAI_TEST_POSTGRES")
    if not address:
        pytest.skip("Set a disposable local PostgreSQL URL.")
    parsed = urlsplit(address)
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "moyai_checkpoint_" + uuid4().hex
    admin = await asyncpg.connect(address)
    await admin.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
    settings = Settings(
        _env_file=None, data_dir=tmp_path,
        database_url=urlunsplit(parsed._replace(path="/" + name)),
        workspace_password="test-password-12345", default_model="test",
        encryption_key="local-checkpoint-race-test-key-for-isolated-fixtures-only",
    )
    database = Database(settings, Security(settings.master_key))
    try:
        await database.start()
        async with database.sessions() as db:
            actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await database.create_session(actor, NewSession())
        turn = await database.submit(actor, session.id, NewMessage(prompt="Pause safely.", request_key="race"))
        await database.claim_next()
        prepared = await database.prepare(turn.id)
        await database.save_result(
            prepared.scope,
            conversation=ConversationTypeAdapter.dump_json(Conversation()).decode(),
            output="", workspace=REFERENCE, usage={}, deferred=True, checkpoint_workspace=True,
        )
        yield database, actor, session, turn, admin, name
    finally:
        await database.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.parametrize("settlement", ["settle", "stopped"])
async def test_checkpoint_fallback_serializes_with_stop(checkpoint_postgres, monkeypatch, settlement):
    database, actor, session, turn, admin, name = checkpoint_postgres
    observed, release = asyncio.Event(), asyncio.Event()
    original_get = AsyncSession.get
    settling = None

    async def pause_after_read(db, model, identity, *args, **kwargs):
        result = await original_get(db, model, identity, *args, **kwargs)
        if asyncio.current_task() is settling and model is Session:
            observed.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncSession, "get", pause_after_read)
    settling = asyncio.create_task(getattr(database, settlement)(turn.id))
    stopping = None
    try:
        await asyncio.wait_for(observed.wait(), 5)
        stopping = asyncio.create_task(database.stop(actor, session.id))

        async def stop_is_waiting_on_row():
            while not await admin.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=$1 AND wait_event_type='Lock')", name,
            ):
                if stopping.done():
                    raise AssertionError("Stop bypassed the settlement row lock.")
                await asyncio.sleep(0.01)

        await asyncio.wait_for(stop_is_waiting_on_row(), 5)
    finally:
        release.set()
        await settling
        if stopping is not None:
            await stopping
    async with database.sessions() as db:
        current, saved_turn = await db.get(Session, session.id), await db.get(Turn, turn.id)
        assert current.status == saved_turn.status == "stopped"
        assert saved_turn.result_accepted and saved_turn.conversation
    await database.stopped(turn.id)
    async with database.sessions() as db:
        assert (await db.get(Session, session.id)).active_turn is None
