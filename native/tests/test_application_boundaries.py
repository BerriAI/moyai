"""Supplementary application guarantees; live gateway/Modal proof is separate."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import func, select
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment

from moyai.api import create_app
from moyai.contracts import Grant, NewMessage, NewSession
from moyai.database import Database
from moyai.effects import EffectReceipts
from moyai.models import Connection, Effect, Message, Session, Turn, User, new_id
from moyai.security import Security
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import dispatch_once, make_worker
from moyai.workflow import TurnWorkflow


@asynccontextmanager
async def sign_in(runtime):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    )
    response = await client.post("/api/login", json={
        "username": "admin", "password": "test-password-12345",
    })
    assert response.status_code == 200
    client.headers["X-Moyai-CSRF"] = response.json()["csrf"]
    try:
        yield client
    finally:
        await client.aclose()


async def test_answer_commit_can_be_retried_after_lost_acknowledgment(runtime, active):
    _session, turn, prepared = active
    arguments = {
        "conversation": ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id
        )).decode(),
        "output": "Saved answer.", "workspace": None, "usage": {"requests": 1},
    }
    assert await runtime.database.save_result(prepared.scope, **arguments)
    assert await runtime.database.save_result(prepared.scope, **arguments)
    await runtime.database.settle(turn.id)
    assert await runtime.database.save_result(prepared.scope, **arguments)
    with pytest.raises(ValueError, match="immutable"):
        await runtime.database.save_result(prepared.scope, **{**arguments, "output": "A different answer"})


@pytest.mark.parametrize("cleanup", ["fail", "reconcile", "stop"])
async def test_snapshot_cleanup_preserves_published_answer_and_releases_queue(runtime, active, actor, cleanup):
    session, turn, prepared = active
    await runtime.database.save_result(
        prepared.scope,
        conversation=ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id
        )).decode(),
        output="The committed answer.", workspace=None, usage={"requests": 1},
    )
    assert turn.id in {item.id for item in await runtime.database.unsettled_turns()}
    if cleanup == "stop":
        await runtime.database.stop(actor, session.id)
        await runtime.database.stopped(turn.id)
    else:
        await runtime.database.fail(turn.id, "Snapshot worker disappeared.", only_unsettled=cleanup == "reconcile")
    # A repeated failure/cleanup receipt must not downgrade the accepted answer.
    await runtime.database.fail(turn.id, "A later cleanup retry.")
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        current = await db.get(Session, session.id)
        assert saved.status == "completed" and saved.error is None
        assert runtime.security.unseal(saved.result).decode() == "The committed answer."
        assert current.active_turn is None and current.save_warning
        assert current.status == ("stopped" if cleanup == "stop" else "idle")
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.identity == f"answer:{turn.id}"
        )) == 1
    following = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Continue.", request_key="after-snapshot-failure"),
    )
    assert (await runtime.database.claim_next()).id == following.id


@pytest.mark.parametrize("cleanup", ["snapshot", "fail", "stopped"])
async def test_completed_answer_settlement_preserves_stop_and_cancelled_queue(runtime, active, actor, cleanup):
    session, turn, prepared = active
    queued = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Queued work must stay cancelled.", request_key="queued-before-stop",
    ))
    await runtime.database.save_result(
        prepared.scope, conversation="{}", output="Already received.",
        workspace=None, usage={"requests": 1},
    )
    await runtime.database.stop(actor, session.id)
    if cleanup == "snapshot":
        await runtime.database.settle(turn.id, snapshot="late-image")
    elif cleanup == "fail":
        await runtime.database.fail(turn.id, "Snapshot cleanup failed.")
    else:
        await runtime.database.stopped(turn.id)
    async with runtime.database.sessions() as db:
        current = await db.get(Session, session.id)
        accepted = await db.get(Turn, turn.id)
        assert current.status == "stopped" and current.active_turn is None
        assert current.snapshot is None
        assert accepted.status == "completed" and accepted.result_accepted
        assert runtime.security.unseal(accepted.result).decode() == "Already received."
        assert (await db.get(Turn, queued.id)).status == "stopped"
    assert await runtime.database.claim_next() is None


async def test_pin_preference_round_trips_through_session_snapshot(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as client:
        for pinned in (False, True, False):
            response = await client.patch(f"/api/sessions/{session.id}", json={"pinned": pinned})
            assert response.status_code == 200
            view = (await client.get(f"/api/sessions/{session.id}")).json()
            assert view["session"]["pinned"] is pinned
            listing = (await client.get("/api/sessions")).json()
            assert next(row for row in listing if row["id"] == session.id)["pinned"] is pinned


async def test_native_search_and_memory_scopes_change_with_account_grants(runtime, active):
    _session, _turn, prepared = active
    original = prepared.scope
    before = original.conversation_id, original.memory_namespace
    original.grants.append(Grant("account", "github", 1, "encrypted"))
    assert (original.conversation_id, original.memory_namespace) != before
    granted = original.conversation_id, original.memory_namespace
    original.grants[0].revision = 2
    assert (original.conversation_id, original.memory_namespace) != granted


async def test_personal_accounts_cannot_feed_shared_conversations(runtime, actor):
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Personal",
            credential=runtime.security.seal("local-test-token"),
        )
        db.add(connection)
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    with pytest.raises(HTTPException) as error:
        await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Use my personal account", connections=[connection.id], request_key="private-in-shared"
        ))
    assert error.value.status_code == 403


async def test_existing_computer_cannot_be_reused_under_changed_accounts(runtime, actor, active):
    session, turn, prepared = active
    await runtime.database.save_workspace_reference(prepared.scope, {"provider": "modal", "id": "owned"})
    await runtime.database.stopped(turn.id)
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Another account",
            credential=runtime.security.seal("local-test-token"),
        )
        db.add(connection)
    with pytest.raises(HTTPException) as error:
        await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Change accounts", connections=[connection.id], request_key="different-account"
        ))
    assert error.value.status_code == 409


async def test_object_writes_are_immutable_and_deduplicated(runtime):
    a, b = await asyncio.gather(
        runtime.objects.put(b"same private contents"), runtime.objects.put(b"same private contents")
    )
    assert a == b
    assert await runtime.objects.get(a) == b"same private contents"
    assert len(list(runtime.objects.directory.rglob(a))) == 1


async def test_parallel_api_worker_bootstrap_has_one_key_and_administrator(tmp_path):
    configurations = [
        Settings(
            _env_file=None, data_dir=tmp_path, encryption_key="", database_url="",
            workspace_password="test-password-12345",
        ) for _ in range(2)
    ]
    keys = await asyncio.gather(*(
        asyncio.to_thread(lambda settings=settings: settings.master_key) for settings in configurations
    ))
    assert keys[0] == keys[1]
    databases = [Database(settings, Security(key)) for settings, key in zip(configurations, keys, strict=True)]
    try:
        await asyncio.gather(*(database.start() for database in databases))
        async with databases[0].sessions() as db:
            assert await db.scalar(select(func.count()).select_from(User)) == 1
    finally:
        await asyncio.gather(*(database.close() for database in databases))


async def test_login_attempt_limit_is_shared_by_application_instances(runtime, monkeypatch):
    monkeypatch.setattr("moyai.database.time.time", lambda: 1_800_000_000)
    for _ in range(10):
        await runtime.database.limit_login("127.0.0.1", "admin")
    other = Database(runtime.settings, runtime.security)
    try:
        with pytest.raises(HTTPException) as error:
            await other.limit_login("127.0.0.1", "admin")
        assert error.value.status_code == 429
    finally:
        await other.close()


async def test_uncertain_write_blocks_new_call_ids_until_reviewed(runtime, active, actor):
    session, turn, prepared = active
    receipts = EffectReceipts(runtime.database)
    await receipts.reserve(prepared.scope, "first-call", "shell", {"command": "external write"})
    with pytest.raises(PermissionError, match="reconciliation"):
        await receipts.reserve(prepared.scope, "new-call", "shell", {"command": "different spelling"})
    await runtime.database.fail(turn.id, "The transport lost the acknowledgment.")
    async with sign_in(runtime) as client:
        pending = await client.get(f"/api/sessions/{session.id}/effects")
        assert pending.json()[0]["id"] == "first-call"
        resolved = await client.post("/api/effects/first-call/resolve", json={
            "outcome": "completed", "evidence": "Checked the destination and found the created item.",
        })
        assert resolved.status_code == 200
    follow_up = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Continue.", request_key="after-review"
    ))
    await runtime.database.claim_next()
    admitted = await runtime.database.prepare(follow_up.id)
    await receipts.reserve(admitted.scope, "next-action", "shell", {"command": "next operation"})


async def test_stop_before_dispatch_releases_only_the_unstarted_turn(runtime, actor, active):
    session, _turn, _prepared = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects)
    )
    async with environment:
        runtime.client = environment.client
        await runtime.database.stop(actor, session.id)
        await dispatch_once(runtime)
    saved = await runtime.database.session(actor, session.id)
    assert saved.active_turn is None and saved.status == "stopped"


@pytest.mark.parametrize("runtime", ["question"], indirect=True)
async def test_native_question_reply_is_idempotent_through_api(runtime, actor, active):
    session, turn, _prepared = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects)
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            result = await runtime.client.execute_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            assert result["status"] == "waiting"
            async with sign_in(runtime) as client:
                view = (await client.get(f"/api/sessions/{session.id}")).json()
                call = view["pending"]["calls"][0]
                body = {
                    "turn_id": turn.id, "request_key": "one-answer",
                    "answers": {call["tool_call_id"]: {"answers": [{"header": "Choice", "selected": ["Text"]}]}},
                }
                first = await client.post(f"/api/sessions/{session.id}/answers", json=body)
                assert first.status_code == 202, first.text
                second = await client.post(f"/api/sessions/{session.id}/answers", json=body)
                assert second.status_code == 202, second.text
                assert first.json()["id"] == second.json()["id"]
            await runtime.database.claim_next()
            async with runtime.database.sessions() as db:
                resumed = await db.get(Turn, first.json()["id"])
            finished = await runtime.client.execute_workflow(
                TurnWorkflow.run, resumed.id, id=resumed.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            assert finished["status"] == "completed"


@pytest.mark.parametrize("runtime", ["stop"], indirect=True)
async def test_stop_cancels_native_command_and_preserves_uncertainty(runtime, actor, active):
    session, turn, _prepared = active
    computer = runtime.settings.data_dir.parent / "computer"
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects)
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            async with asyncio.timeout(20):
                while not (computer / "started.txt").exists():
                    await asyncio.sleep(0.05)
            async with sign_in(runtime) as client:
                response = await client.post(f"/api/sessions/{session.id}/stop")
                assert response.status_code == 202
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 20)
            assert not (computer / "forbidden.txt").exists()
            saved = await runtime.database.session(actor, session.id)
            assert saved.status == "stopped" and saved.active_turn is None
            async with runtime.database.sessions() as db:
                unknown = await db.scalar(select(Effect).where(Effect.turn_id == turn.id))
                assert unknown is not None and unknown.state == "pending"


@pytest.mark.parametrize("runtime", ["steer"], indirect=True)
async def test_native_steering_fences_old_action_and_authorizes_new_delegate(
    runtime, actor, active, model_gates
):
    session, turn, _prepared = active
    computer = runtime.settings.data_dir.parent / "computer"
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects)
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(model_gates[0].wait(), 20)
            correction = await runtime.database.submit(actor, session.id, NewMessage(
                prompt="NEW GUIDANCE: delegate the corrected work.",
                request_key="urgent-correction", mode="asap",
            ))
            await handle.signal(TurnWorkflow.steer, correction.id)
            model_gates[1].set()
            result = await asyncio.wait_for(handle.result(), 30)
            assert result["status"] == "completed"
    assert (computer / "steered.txt").read_text() == "corrected"
    assert not (computer / "forbidden-old.txt").exists()
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, correction.id)).status == "included"
