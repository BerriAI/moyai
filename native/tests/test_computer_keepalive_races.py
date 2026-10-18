"""Exercise the actual authorization fence while PostgreSQL changes lifecycle state."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from pydantic_ai import Conversation, ConversationTypeAdapter
from sqlalchemy import select

from moyai.computer_keepalive import keep_computer_alive
from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.models import Session, Turn, User
from moyai.security import Security
from moyai.settings import Settings
from moyai.workspace_recovery import locked_scope

REFERENCE = {"provider": "modal", "id": "sb-transition-race"}


@pytest_asyncio.fixture
async def race(tmp_path, monkeypatch):
    address = os.getenv("MOYAI_TEST_POSTGRES")
    if not address:
        pytest.skip("Set a disposable local PostgreSQL URL.")
    parsed = urlsplit(address)
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    name = "moyai_keepalive_" + uuid4().hex
    admin = await asyncpg.connect(address)
    await admin.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
    url = urlunsplit(parsed._replace(path="/" + name))
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=url, default_model="test",
        workspace_password="local-test-password", computer_idle_timeout=60,
        encryption_key="isolated-keepalive-postgres-test-key-only",
    )
    database = Database(settings, Security(settings.master_key))
    blocker = None
    try:
        await database.start()
        async with database.sessions() as db:
            actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await database.create_session(actor, NewSession())
        turn = await database.submit(actor, session.id, NewMessage(prompt="Save my work.", request_key="race"))
        await database.claim_next()
        prepared = await database.prepare(turn.id)
        await database.save_workspace_reference(prepared.scope, REFERENCE)
        process = SimpleNamespace(wait=SimpleNamespace(aio=AsyncMock(return_value=0)))
        sandbox = SimpleNamespace(
            poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
            exec=SimpleNamespace(aio=AsyncMock(return_value=process)),
        )
        attach = AsyncMock(return_value=sandbox)
        monkeypatch.setattr("moyai.computer_keepalive.modal.Sandbox.from_id.aio", attach)
        monkeypatch.setattr("moyai.runtime.current", lambda: SimpleNamespace(database=database, settings=settings))
        blocker = await asyncpg.connect(url)
        yield SimpleNamespace(
            database=database, actor=actor, session=session, turn=turn, prepared=prepared,
            admin=admin, blocker=blocker, name=name, sandbox=sandbox, attach=attach,
        )
    finally:
        if blocker is not None:
            await blocker.close()
        await database.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def wait_for_lock(race, task: asyncio.Task, table: str) -> None:
    async with asyncio.timeout(5):
        while not await race.admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
            "WHERE datname=$1 AND wait_event_type='Lock' AND query LIKE $2)",
            race.name, f"UPDATE {table} %",
        ):
            if task.done():
                await task
                raise AssertionError("The operation bypassed the expected row lock.")
            await asyncio.sleep(.01)


async def accept_answer(race, *, deferred: bool = False, checkpoint: bool = True) -> None:
    assert await race.database.save_result(
        race.prepared.scope,
        conversation=ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=race.prepared.scope.conversation_id,
        )).decode(),
        output="" if deferred else "Saved.", workspace=REFERENCE, usage={},
        deferred=deferred, checkpoint_workspace=checkpoint,
    )


@pytest.mark.parametrize("transition", [
    "saving", "completed", "waiting", "stopped", "revoked", "superseded",
])
async def test_tick_checks_phase_and_authority_after_waiting_for_actor_lock(race, transition):
    transaction = race.blocker.transaction()
    await transaction.start()
    await race.blocker.execute("SELECT id FROM moyai_users WHERE id=$1 FOR NO KEY UPDATE", race.actor.id)
    ticking = asyncio.create_task(keep_computer_alive(race.prepared.scope))
    try:
        # On the old code, the tick has already read "running" here. The answer
        # transaction does not need this actor lock and can commit "saving" first.
        await wait_for_lock(race, ticking, "moyai_users")
        async with asyncio.timeout(5):
            if transition in {"saving", "completed", "waiting"}:
                await accept_answer(race, deferred=transition == "waiting", checkpoint=transition == "saving")
                if transition == "completed":
                    await race.database.settle(race.turn.id)
            elif transition == "stopped":
                await race.database.stop(race.actor, race.session.id)
            elif transition == "revoked":
                await race.blocker.execute("UPDATE moyai_users SET enabled=false WHERE id=$1", race.actor.id)
            else:
                await race.blocker.execute(
                    "UPDATE moyai_sessions SET active_turn=NULL WHERE id=$1", race.session.id,
                )
    finally:
        await transaction.commit()
    result = await asyncio.wait_for(ticking, 5)
    assert result.active is (transition == "saving")
    if transition == "saving":
        race.sandbox.exec.aio.assert_awaited_once_with("/bin/true", timeout=10)
        async with race.database.sessions() as db:
            session = await db.get(Session, race.session.id)
            turn = await db.get(Turn, race.turn.id)
            assert session.status == turn.status == "saving"
            assert turn.result_accepted and session.snapshot
    else:
        race.attach.assert_not_awaited()


async def test_answer_waits_for_tick_fence_then_next_tick_accepts_saving(race, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = race.database.scope_grants
    ticking = None

    async def hold_fence(*args):
        result = await original(*args)
        if asyncio.current_task() is ticking:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(race.database, "scope_grants", hold_fence)
    ticking = asyncio.create_task(keep_computer_alive(race.prepared.scope))
    saving = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        saving = asyncio.create_task(accept_answer(race))
        await wait_for_lock(race, saving, "moyai_sessions")
    finally:
        release.set()
        assert (await asyncio.wait_for(ticking, 5)).active
        if saving is not None:
            await asyncio.wait_for(saving, 5)
    assert (await keep_computer_alive(race.prepared.scope)).active
    assert race.sandbox.exec.aio.await_count == 2


async def test_phase_specific_fences_stay_strict(race):
    await accept_answer(race)
    async with race.database.sessions.begin() as db:
        with pytest.raises(PermissionError):
            await locked_scope(race.database, db, race.prepared.scope, status="running")
    async with race.database.sessions.begin() as db:
        session, turn = await locked_scope(race.database, db, race.prepared.scope, status="saving")
        assert session.status == turn.status == "saving"


async def test_session_and_turn_must_agree_even_when_both_phases_are_allowed(race):
    await race.blocker.execute("UPDATE moyai_sessions SET status='saving' WHERE id=$1", race.session.id)
    assert not (await keep_computer_alive(race.prepared.scope)).active
    race.attach.assert_not_awaited()
