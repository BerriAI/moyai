"""Recoverable-delete regression tests; these supplement deployed-app E2E proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.testing import WorkflowEnvironment
from test_application_boundaries import sign_in
from test_images_and_computer import image_bytes
from test_sidebar import member_client

from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.models import Artifact, ComputerOperation, Message, Session, Turn, Upload, User, new_id
from moyai.security import Security
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import dispatch_once


async def saved_artifact(runtime, session, turn):
    content = b"Preserved session deliverable."
    digest = await runtime.objects.put(content)
    async with runtime.database.sessions.begin() as db:
        row = Artifact(
            id=new_id(), session_id=session.id, turn_id=turn.id, name="saved.txt",
            media_type="text/plain", digest=digest, content_hash=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
        db.add(row)
    return row, content


async def test_only_owner_can_delete_restore_and_see_trash_even_for_shared_session(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with (
        sign_in(runtime) as owner,
        member_client(runtime) as (member, _),
        member_client(runtime) as (admin, other_admin),
    ):
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, other_admin.id)).role = "admin"
        path = f"/api/sessions/{session.id}"
        for client in (member, admin):
            assert (await client.get(path)).status_code == 200
            assert (await client.delete(path)).status_code == 404
        for _ in range(2):
            assert (await owner.delete(path)).status_code == 200
        for client in (owner, member, admin):
            assert (await client.get(path)).status_code == 404
            assert (await client.get("/api/sessions")).json() == []
        trash = (await owner.get("/api/sessions?deleted=true")).json()
        assert [row["id"] for row in trash] == [session.id]
        for client in (member, admin):
            assert (await client.get("/api/sessions?deleted=true")).json() == []
            assert (await client.post(f"{path}/restore")).status_code == 404
        for _ in range(2):
            assert (await owner.post(f"{path}/restore")).status_code == 200
        assert (await owner.get("/api/sessions?deleted=true")).json() == []
        assert (await member.get(path)).status_code == 200


async def test_delete_hides_resources_rejects_writes_and_restore_preserves_history_without_queue(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with sign_in(runtime) as owner, member_client(runtime) as (viewer, _):
        image = (await owner.post("/api/uploads?name=kept.png", content=image_bytes())).json()
        turn = await runtime.database.submit(actor, session.id, NewMessage(
            prompt="Keep this history.", attachments=[image["id"]], request_key="queued-before-delete",
        ))
        artifact, content = await saved_artifact(runtime, session, turn)
        path = f"/api/sessions/{session.id}"
        link = (await owner.post(f"{path}/links", json={
            "kind": "output", "url": "https://example.invalid/preserved",
        })).json()
        original_messages = (await owner.get(f"{path}/messages")).json()
        async with runtime.database.sessions.begin() as db:
            saved = await db.get(Session, session.id)
            saved.archived = True
            saved.workspace = json.dumps({"provider": "modal", "id": "retained-reference-only"})
            saved.conversation = runtime.security.seal("retained native conversation")
        assert (await owner.delete(path)).status_code == 200
        assert await runtime.database.claim_next() is None
        assert [row["id"] for row in (await owner.get("/api/sessions?deleted=true")).json()] == [session.id]
        for client in (owner, viewer):
            for url in (
                path, f"{path}/messages", f"{path}/workspace", f"{path}/files",
                f"/api/files/{artifact.id}", f"/api/uploads/{image['id']}", f"{path}/computer",
            ):
                assert (await client.get(url)).status_code == 404, url
            for key in ("queued-before-delete", "new-message"):
                response = await client.post(f"{path}/messages", json={
                    "prompt": "Keep this history.", "request_key": key,
                })
                assert response.status_code == 404
            assert (await client.post(f"{path}/links", json={
                "kind": "source", "url": "https://example.invalid/new",
            })).status_code == 404
            assert (await client.delete(f"{path}/links/{link['id']}")).status_code == 404
        assert (await owner.post(f"{path}/restore")).status_code == 200
        assert (await owner.get(f"{path}/messages")).json() == original_messages
        assert (await viewer.get(f"/api/files/{artifact.id}")).content == content
        assert (await viewer.get(f"/api/uploads/{image['id']}")).content == image_bytes()
        overview = (await owner.get(f"{path}/workspace")).json()
        assert [row["id"] for row in overview["links"]] == [link["id"]]
        assert await runtime.database.claim_next() is None
        async with runtime.database.sessions() as db:
            saved = await db.get(Session, session.id)
            assert not saved.deleted and not saved.archived
            assert json.loads(saved.workspace)["id"] == "retained-reference-only"
            assert runtime.security.unseal(saved.conversation).decode() == "retained native conversation"
            assert (await db.get(Turn, turn.id)).status == "stopped"


async def test_active_delete_revokes_scope_and_waits_for_existing_stop_dispatcher(runtime, active, actor):
    session, turn, prepared = active
    queued = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Queued work must stay stopped.", request_key="queued",
    ))
    await runtime.database.delete_session(actor, session.id)
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.database.authorize_scope(prepared.scope)
    assert [row.id for row in await runtime.database.pending_stops()] == [turn.id]
    with pytest.raises(HTTPException) as caught:
        await runtime.database.restore_session(actor, session.id)
    assert caught.value.status_code == 409
    assert await runtime.database.claim_next() is None
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    )
    async with environment:
        runtime.client = environment.client
        await dispatch_once(runtime)
    await runtime.database.restore_session(actor, session.id)
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "stopped"
        assert (await db.get(Turn, queued.id)).status == "stopped"
        assert (await db.get(Session, session.id)).active_turn is None
    assert await runtime.database.claim_next() is None
    following = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Explicit new work after restore.", request_key="after-restore",
    ))
    assert (await runtime.database.claim_next()).id == following.id


@pytest.mark.parametrize("pending_seconds", [60, -1])
async def test_restore_waits_for_desktop_operation_but_not_expired_receipt(runtime, actor, pending_seconds):
    session = await runtime.database.create_session(actor, NewSession())
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerOperation(
            session_id=session.id, identity=new_id(), expires_at=int(time.time()) + pending_seconds,
        ))
    await runtime.database.delete_session(actor, session.id)
    if pending_seconds > 0:
        with pytest.raises(HTTPException) as caught:
            await runtime.database.restore_session(actor, session.id)
        assert caught.value.status_code == 409
        with pytest.raises(HTTPException) as caught:
            await runtime.database.submit(actor, session.id, NewMessage(prompt="Too soon", request_key="early"))
        assert caught.value.status_code == 404
        async with runtime.database.sessions.begin() as db:
            await db.delete(await db.get(ComputerOperation, session.id))
    await runtime.database.restore_session(actor, session.id)
    assert not (await runtime.database.session(actor, session.id)).deleted


async def test_delete_during_snapshot_settlement_preserves_confirmed_answer(runtime, actor, active):
    session, turn, prepared = active
    await runtime.database.save_result(
        prepared.scope,
        conversation=ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id,
        )).decode(),
        output="Confirmed saved answer.", workspace=None, usage={"requests": 1},
    )
    await runtime.database.delete_session(actor, session.id)
    with pytest.raises(HTTPException) as caught:
        await runtime.database.restore_session(actor, session.id)
    assert caught.value.status_code == 409
    await runtime.database.stopped(turn.id)
    await runtime.database.fail(turn.id, "Late snapshot cleanup failure")
    await runtime.database.restore_session(actor, session.id)
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "completed" and saved.result_accepted and saved.error is None
        assert runtime.security.unseal(saved.result).decode() == "Confirmed saved answer."
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.identity == f"answer:{turn.id}",
        )) == 1
        assert (await db.get(Session, session.id)).active_turn is None
    assert (await runtime.database.messages(actor, session.id))[-1]["content"] == "Confirmed saved answer."


@pytest.mark.parametrize("resource", ["file", "image"])
async def test_download_rechecks_deleted_session_after_object_read(runtime, actor, monkeypatch, resource):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Task", request_key="file"))
    artifact, content = await saved_artifact(runtime, session, turn)
    async with runtime.database.sessions.begin() as db:
        image = Upload(
            id=new_id(), owner_id=actor.id, session_id=session.id, name="image.png",
            media_type="image/png", digest=artifact.digest, size=len(content),
        )
        db.add(image)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.objects.get

    async def delayed(digest):
        data = await original(digest)
        entered.set()
        await release.wait()
        return data

    monkeypatch.setattr(runtime.objects, "get", delayed)
    async with sign_in(runtime) as client:
        url = f"/api/files/{artifact.id}" if resource == "file" else f"/api/uploads/{image.id}"
        request = asyncio.create_task(client.get(url))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await runtime.database.delete_session(actor, session.id)
            release.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code == 404
            assert content not in response.content
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


async def test_image_download_rechecks_upload_bound_to_deleted_session_during_read(runtime, actor, monkeypatch):
    session = await runtime.database.create_session(actor, NewSession())
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.objects.get

    async def delayed(digest):
        data = await original(digest)
        entered.set()
        await release.wait()
        return data

    async with sign_in(runtime) as client:
        response = await client.post("/api/uploads?name=initially-unbound.png", content=image_bytes())
        assert response.status_code == 201
        image = response.json()
        monkeypatch.setattr(runtime.objects, "get", delayed)
        request = asyncio.create_task(client.get(f"/api/uploads/{image['id']}"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await runtime.database.submit(actor, session.id, NewMessage(
                prompt="Bind the image while its download waits.",
                attachments=[image["id"]], request_key="bind-during-read",
            ))
            await runtime.database.delete_session(actor, session.id)
            release.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code == 404
            assert image_bytes() not in response.content
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@asynccontextmanager
async def independent_databases(runtime, tmp_path, backend):
    """Use separate pools, including a disposable database for optional local PostgreSQL."""
    if backend == "sqlite":
        other = Database(runtime.settings, runtime.security)
        try:
            yield runtime.database, other
        finally:
            await other.close()
        return
    url = os.environ.get("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Set MOYAI_TEST_POSTGRES to a disposable local PostgreSQL server.")
    base = urlsplit(url)
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    admin = await asyncpg.connect(urlunsplit(base))
    name = f"moyai_deletion_{uuid4().hex}"
    await admin.execute(f'CREATE DATABASE {name} ENCODING \'UTF8\' TEMPLATE template0')
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "postgres",
        database_url=urlunsplit(base._replace(path=f"/{name}")),
        encryption_key="isolated-deletion-test-key-for-postgres-only",
        workspace_password="test-password-12345",
        default_model="test",
    )
    security = Security(settings.master_key)
    first, second = Database(settings, security), Database(settings, security)
    try:
        await first.start()
        yield first, second
    finally:
        await first.close()
        await second.close()
        await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_delete_between_authorization_and_admission_cannot_leave_queued_work(
    runtime, actor, tmp_path, monkeypatch, backend,
):
    async with independent_databases(runtime, tmp_path, backend) as (submitting, deleting):
        if backend == "postgres":
            async with submitting.sessions() as db:
                actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await submitting.create_session(actor, NewSession())
        checked, release = asyncio.Event(), asyncio.Event()
        original = submitting._submit

        async def delayed(user, session_id, body, fingerprint):
            # Simulate access checked before admission, then let deletion win.
            # Pause before _submit's first write: its User fence now holds the
            # SQLite writer lock, so deletion cannot commit after that point.
            await submitting.session(user, session_id)
            checked.set()
            await release.wait()
            return await original(user, session_id, body, fingerprint)

        monkeypatch.setattr(submitting, "_submit", delayed)
        task = asyncio.create_task(submitting.submit(actor, session.id, NewMessage(
            prompt="Racing deletion", request_key="delete-wins",
        )))
        try:
            await asyncio.wait_for(checked.wait(), 5)
            await asyncio.wait_for(deleting.delete_session(actor, session.id), 5)
            release.set()
            with pytest.raises(HTTPException) as caught:
                await asyncio.wait_for(task, 5)
            assert caught.value.status_code == 404
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        async with deleting.sessions() as db:
            assert (await db.get(Session, session.id)).deleted
            assert await db.scalar(select(func.count()).select_from(Turn).where(
                Turn.session_id == session.id,
            )) == 0
            assert await db.scalar(select(func.count()).select_from(Message).where(
                Message.session_id == session.id,
            )) == 0
        assert await deleting.claim_next() is None
        await deleting.restore_session(actor, session.id)
        assert await deleting.claim_next() is None


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_admission_holding_row_fence_is_stopped_by_concurrent_delete(
    runtime, actor, tmp_path, monkeypatch, backend,
):
    async with independent_databases(runtime, tmp_path, backend) as (submitting, deleting):
        if backend == "postgres":
            async with submitting.sessions() as db:
                actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await submitting.create_session(actor, NewSession())
        admitted, release, deletion_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = submitting.resolve_grants

        async def delayed(*args, **kwargs):
            grants = await original(*args, **kwargs)
            admitted.set()
            await release.wait()
            return grants

        async def delete():
            deletion_started.set()
            await deleting.delete_session(actor, session.id)

        monkeypatch.setattr(submitting, "resolve_grants", delayed)
        sending = asyncio.create_task(submitting.submit(actor, session.id, NewMessage(
            prompt="Admitted before deletion", request_key="submit-wins",
        )))
        deletion = None
        try:
            await asyncio.wait_for(admitted.wait(), 5)
            deletion = asyncio.create_task(delete())
            await asyncio.wait_for(deletion_started.wait(), 5)
            release.set()
            turn, _ = await asyncio.wait_for(asyncio.gather(sending, deletion), 10)
        finally:
            release.set()
            await asyncio.gather(sending, *([deletion] if deletion else []), return_exceptions=True)
        async with deleting.sessions() as db:
            assert (await db.get(Session, session.id)).deleted
            assert (await db.get(Turn, turn.id)).status == "stopped"
        await deleting.restore_session(actor, session.id)
        assert await deleting.claim_next() is None


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_delete_during_preparation_cannot_reanimate_stopped_turn(
    runtime, actor, tmp_path, monkeypatch, backend,
):
    async with independent_databases(runtime, tmp_path, backend) as (preparing, deleting):
        if backend == "postgres":
            async with preparing.sessions() as db:
                actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await preparing.create_session(actor, NewSession())
        turn = await preparing.submit(actor, session.id, NewMessage(
            prompt="Prepare while deletion starts", request_key="preparation-race",
        ))
        assert (await preparing.claim_next()).id == turn.id
        checked, release, deletion_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = preparing.resolve_grants

        async def delayed(*args, **kwargs):
            grants = await original(*args, **kwargs)
            checked.set()
            await release.wait()
            return grants

        async def delete():
            deletion_started.set()
            await deleting.delete_session(actor, session.id)

        monkeypatch.setattr(preparing, "resolve_grants", delayed)
        preparation = asyncio.create_task(preparing.prepare(turn.id))
        deletion = None
        try:
            await asyncio.wait_for(checked.wait(), 5)
            deletion = asyncio.create_task(delete())
            await asyncio.wait_for(deletion_started.wait(), 5)
            # If prepare fences the row, deletion must wait. Otherwise deliberately
            # let deletion commit after prepare's access check but before its writes.
            await asyncio.wait({deletion}, timeout=0.1)
            release.set()
            outcomes = await asyncio.wait_for(
                asyncio.gather(preparation, deletion, return_exceptions=True), 10,
            )
            assert outcomes[1] is None
            if isinstance(outcomes[0], BaseException):
                assert isinstance(outcomes[0], (HTTPException, PermissionError))
        finally:
            release.set()
            await asyncio.gather(
                preparation, *([deletion] if deletion else []), return_exceptions=True,
            )
        async with deleting.sessions() as db:
            saved = await db.get(Session, session.id)
            assert saved.deleted and saved.status == "stopped"
            assert (await db.get(Turn, turn.id)).status == "stopped"
        assert [row.id for row in await deleting.pending_stops()] == [turn.id]


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("winner", ["stop", "delete", "saved_answer"])
async def test_failure_rechecks_turn_after_concurrent_terminal_decision(
    runtime, actor, tmp_path, monkeypatch, backend, winner,
):
    async with independent_databases(runtime, tmp_path, backend) as (failing, deciding):
        if backend == "postgres":
            async with failing.sessions() as db:
                actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await failing.create_session(actor, NewSession())
        turn = await failing.submit(actor, session.id, NewMessage(prompt="Race", request_key="failure-race"))
        assert (await failing.claim_next()).id == turn.id
        prepared = await failing.prepare(turn.id)
        read, release = asyncio.Event(), asyncio.Event()
        original_get, original_scalar = AsyncSession.get, AsyncSession.scalar

        async def pause(db):
            if db.bind is failing.engine and not read.is_set():
                read.set()
                await release.wait()

        async def delayed_get(db, entity, key, **kwargs):
            row = await original_get(db, entity, key, **kwargs)
            if entity is Turn and key == turn.id:
                await pause(db)
            return row

        async def delayed_scalar(db, statement, *args, **kwargs):
            value = await original_scalar(db, statement, *args, **kwargs)
            # The fixed path reads only the session ID before acquiring the
            # fence. The old path instead cached the entire Turn via get().
            if getattr(statement, "is_select", False) and value == session.id:
                await pause(db)
            return value

        monkeypatch.setattr(AsyncSession, "get", delayed_get)
        monkeypatch.setattr(AsyncSession, "scalar", delayed_scalar)
        failure = asyncio.create_task(failing.fail(turn.id, "Late worker exception"))
        try:
            await asyncio.wait_for(read.wait(), 5)
            if winner == "stop":
                await asyncio.wait_for(deciding.stop(actor, session.id), 5)
            elif winner == "delete":
                await asyncio.wait_for(deciding.delete_session(actor, session.id), 5)
            else:
                assert await asyncio.wait_for(deciding.save_result(
                    prepared.scope, conversation="{}", output="Already confirmed",
                    workspace=None, usage={},
                ), 5)
            release.set()
            await asyncio.wait_for(failure, 5)
        finally:
            release.set()
            await asyncio.gather(failure, return_exceptions=True)
        async with deciding.sessions() as db:
            saved = await db.get(Turn, turn.id)
            current = await db.get(Session, session.id)
            assert saved.error is None
            if winner == "saved_answer":
                assert saved.status == "completed" and saved.result_accepted
                assert current.status == "idle" and current.active_turn is None
                assert deciding.security.unseal(saved.result).decode() == "Already confirmed"
                assert await db.scalar(select(func.count()).select_from(Message).where(
                    Message.identity == f"answer:{turn.id}",
                )) == 1
            else:
                assert saved.status == current.status == "stopped"
                assert current.active_turn == turn.id  # Existing dispatcher must still acknowledge Stop.
                assert current.deleted is (winner == "delete")
        if winner != "saved_answer":
            assert [row.id for row in await deciding.pending_stops()] == [turn.id]
            await deciding.stopped(turn.id)
            assert await deciding.claim_next() is None


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_failure_holds_session_fence_before_turn_settlement_without_stop_deadlock(
    runtime, actor, tmp_path, monkeypatch, backend,
):
    async with independent_databases(runtime, tmp_path, backend) as (failing, stopping):
        if backend == "postgres":
            async with failing.sessions() as db:
                actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await failing.create_session(actor, NewSession())
        turn = await failing.submit(actor, session.id, NewMessage(prompt="Race", request_key="failure-first"))
        assert (await failing.claim_next()).id == turn.id
        await failing.prepare(turn.id)
        locked, release, stop_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_get = AsyncSession.get

        async def delayed_get(db, entity, key, **kwargs):
            row = await original_get(db, entity, key, **kwargs)
            if db.bind is failing.engine and entity is Turn and key == turn.id:
                locked.set()
                await release.wait()
            return row

        async def stop():
            stop_started.set()
            return await stopping.stop(actor, session.id)

        monkeypatch.setattr(AsyncSession, "get", delayed_get)
        failure = asyncio.create_task(failing.fail(turn.id, "Failure won the fence"))
        cancellation = None
        try:
            await asyncio.wait_for(locked.wait(), 5)
            cancellation = asyncio.create_task(stop())
            await asyncio.wait_for(stop_started.wait(), 5)
            done, _ = await asyncio.wait({cancellation}, timeout=0.1)
            assert not done  # Stop cannot commit while failure owns the session fence.
            release.set()
            await asyncio.wait_for(asyncio.gather(failure, cancellation), 5)
        finally:
            release.set()
            await asyncio.gather(failure, *([cancellation] if cancellation else []), return_exceptions=True)
        async with stopping.sessions() as db:
            saved = await db.get(Turn, turn.id)
            assert saved.status == "failed" and saved.error == "Failure won the fence"
            assert (await db.get(Session, session.id)).active_turn is None
