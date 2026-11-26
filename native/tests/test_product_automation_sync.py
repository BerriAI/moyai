"""Synchronization failures through the application interface and real PostgreSQL."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
import pytest_asyncio
import test_product_automations as fixtures
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.service import RPCError, RPCStatusCode

from moyai.automations.product_automation_models import ProductAutomationBinding as Binding
from moyai.automations.product_automation_sync import SYNC_CONCURRENCY
from moyai.automations.product_automations import (
    Migration,
    Mutation,
    Save,
    legacy_schedule_id,
    native_schedule_id,
)
from moyai.postgres import create_engine

pytestmark = fixtures.pytestmark
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations


@pytest_asyncio.fixture
async def single_connection_automations(automations, monkeypatch):
    ctx = automations
    database = ctx.product.database
    engine = create_engine(
        database.engine.url, pool_size=1, max_overflow=0, pool_timeout=0.2,
        connect_args={"server_settings": {"search_path": f'"{database.execution_schema}",pg_catalog'}},
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "engine", engine)
            patch.setattr(database, "sessions", async_sessionmaker(engine, expire_on_commit=False))
            patch.setattr(ctx.product.adapter, "native_engine", engine)
            yield ctx
    finally:
        await engine.dispose()


async def test_reconcile_with_one_native_connection_commits_intent_before_remote_create(
    single_connection_automations, monkeypatch,
):
    ctx = single_connection_automations
    row = await fixtures.create(ctx)
    await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="single-connection-sync", activate=True,
    ))
    schedule_id = native_schedule_id(row["id"], "default")
    create_schedule = ctx.remote.create_schedule

    async def verify_committed_intent(identity, schedule, **kwargs):
        async with ctx.product.database.sessions() as db:
            binding = await db.get(Binding, row["id"])
            assert json.loads(binding.schedule_ids) == [schedule_id]
            assert binding.sync_state == "pending" and binding.synced_generation == 0
        await create_schedule(identity, schedule, **kwargs)

    monkeypatch.setattr(ctx.remote, "create_schedule", verify_committed_intent)
    assert (await asyncio.wait_for(ctx.service.reconcile(row["id"]), 3))["status"] == "ready"
    async with ctx.product.database.sessions() as db:
        binding = await db.get(Binding, row["id"])
        assert binding.sync_state == "ready" and binding.synced_generation == binding.generation == 1
    assert not ctx.remote.schedules[schedule_id].state.paused


async def test_sync_connection_bound_applies_before_opening_dedicated_connections(
    single_connection_automations, monkeypatch,
):
    ctx = single_connection_automations
    identities = []
    for _ in range(SYNC_CONCURRENCY + 1):
        row = await fixtures.create(ctx)
        await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
            revision=1, request_key="bounded-sync-" + row["id"], activate=True,
        ))
        identities.append(row["id"])
    creating, release = asyncio.Queue(), asyncio.Event()
    create_schedule = ctx.remote.create_schedule

    async def blocked_create(identity, schedule, **kwargs):
        await creating.put(identity)
        await release.wait()
        await create_schedule(identity, schedule, **kwargs)

    async def connections():
        async with ctx.product.source.connect() as db:
            return await db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            ))

    monkeypatch.setattr(ctx.remote, "create_schedule", blocked_create)
    syncing = [
        asyncio.create_task(ctx.service.reconcile(identity))
        for identity in identities[:SYNC_CONCURRENCY]
    ]
    try:
        for _ in syncing:
            await asyncio.wait_for(creating.get(), 3)
        before_waiter = await connections()
        syncing.append(asyncio.create_task(ctx.service.reconcile(identities[-1])))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(creating.get(), 0.2)
        assert not syncing[-1].done()
        assert await connections() == before_waiter
        async with ctx.product.source.connect() as db:
            assert await db.scalar(text("""
                SELECT count(*) FROM pg_locks
                WHERE locktype='advisory' AND objsubid=1 AND granted
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
            """)) == SYNC_CONCURRENCY
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*syncing), 5)
        assert all(result["status"] == "ready" for result in results)
        assert len(ctx.remote.schedules) == len(identities)
    finally:
        for task in syncing:
            task.cancel()
        await asyncio.gather(*syncing, return_exceptions=True)


@pytest.mark.parametrize("error", [
    OSError("scheduler unavailable"),
    TimeoutError("lost acknowledgment"),
    RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""),
])
async def test_committed_save_and_retry_survive_dispatch_failure(automations, monkeypatch, error):
    ctx = automations
    row = await fixtures.create(ctx)
    request = Migration(revision=1, request_key="recover-pending-intent", activate=True)
    original = ctx.remote.start_workflow

    async def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(ctx.remote, "start_workflow", unavailable)
    saved = await ctx.service.mutate(ctx.grant, "migrate", row["id"], request)
    assert saved["outcome"] == "saved" and saved["sync_state"] == "pending"
    assert await ctx.service.mutate(ctx.grant, "migrate", row["id"], request) == saved
    assert await ctx.service.dispatch_pending() == 0
    assert not ctx.remote.schedules

    monkeypatch.setattr(ctx.remote, "start_workflow", original)
    assert await ctx.service.dispatch_pending() == 1
    assert (await ctx.service.reconcile(row["id"]))["status"] == "ready"
    preview = await ctx.service.preview(ctx.grant, row["id"])
    assert preview["revision"] == saved["revision"] == 2
    assert preview["native"]["generation"] == preview["native"]["synced_generation"] == 1
    assert await ctx.service.dispatch_pending() == 0


async def test_legacy_pause_must_be_confirmed_before_updating_native_schedule(automations):
    ctx = automations
    row, _ = await fixtures.activate(ctx)
    native_id = native_schedule_id(row["id"], "default")
    legacy_id = legacy_schedule_id(row["id"], "default")
    previous = copy.deepcopy(ctx.remote.schedules[native_id])
    ctx.remote.schedules[legacy_id] = copy.deepcopy(previous)
    ctx.remote.pause_confirmed = False
    await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(
        revision=row["revision"], request_key="confirm-legacy-pause",
    ))
    with pytest.raises(RuntimeError, match="Schedule pause is not confirmed"):
        await ctx.service.reconcile(row["id"])
    assert ctx.remote.schedules[native_id] == previous
    assert (await ctx.service.preview(ctx.grant, row["id"]))["native"]["sync_state"] == "pending"
    ctx.remote.pause_confirmed = True
    assert (await ctx.service.reconcile(row["id"]))["status"] == "ready"
    assert ctx.remote.schedules[native_id].state.paused


async def test_lost_create_ack_is_recovered_when_a_later_edit_removes_the_trigger(automations, monkeypatch):
    ctx = automations
    row = await fixtures.create(ctx)
    migrated = await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="lost-creation-ack", activate=True,
    ))
    create_schedule = ctx.remote.create_schedule

    async def lost_ack(identity, schedule, **kwargs):
        await create_schedule(identity, schedule, **kwargs)
        raise TimeoutError("created remotely, acknowledgment lost")

    monkeypatch.setattr(ctx.remote, "create_schedule", lost_ack)
    with pytest.raises(TimeoutError):
        await ctx.service.reconcile(row["id"])
    old_id = native_schedule_id(row["id"], "default")
    assert old_id in ctx.remote.schedules
    assert (await ctx.service.preview(ctx.grant, row["id"]))["native"]["sync_state"] == "pending"

    await ctx.service.mutate(ctx.grant, "update", row["id"], Save(
        revision=migrated["revision"], request_key="replace-after-lost-ack",
        definition=fixtures.definition(triggers=[{
            "id": "replacement", "schedule": {"frequency": "daily", "time": "10:00", "timezone": "UTC"},
        }]),
    ))
    monkeypatch.setattr(ctx.remote, "create_schedule", create_schedule)
    assert (await ctx.service.reconcile(row["id"]))["status"] == "ready"
    assert old_id not in ctx.remote.schedules
    assert ctx.remote.schedules[native_schedule_id(row["id"], "replacement")].state.paused


@pytest.mark.parametrize("mismatch", ["pause", "queue", "generation"])
async def test_remote_readback_must_match_before_intent_is_ready(automations, monkeypatch, mismatch):
    ctx = automations
    row = await fixtures.create(ctx)
    await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="confirm-remote-intent", activate=True,
    ))
    create_schedule = ctx.remote.create_schedule

    async def different_remote_state(identity, schedule, **kwargs):
        await create_schedule(identity, schedule, **kwargs)
        remote = ctx.remote.schedules[identity]
        if mismatch == "pause":
            remote.state.paused = True
        elif mismatch == "queue":
            remote.action.task_queue = "wrong-queue"
        else:
            remote.action.args = [row["id"], 0, 2, "default"]

    monkeypatch.setattr(ctx.remote, "create_schedule", different_remote_state)
    with pytest.raises(RuntimeError, match="Native schedule .* is not confirmed"):
        await ctx.service.reconcile(row["id"])
    assert (await ctx.service.preview(ctx.grant, row["id"]))["native"]["sync_state"] == "pending"
    monkeypatch.setattr(ctx.remote, "create_schedule", create_schedule)
    assert (await ctx.service.reconcile(row["id"]))["status"] == "ready"


async def test_remote_operation_lock_serializes_reconcilers_without_blocking_edits(single_connection_automations):
    ctx = single_connection_automations
    row, _ = await fixtures.activate(ctx)
    ctx.remote.block_update = native_schedule_id(row["id"], "default")
    syncing = asyncio.create_task(ctx.service.reconcile(row["id"]))
    await asyncio.wait_for(ctx.remote.update_started.wait(), 3)
    try:
        with pytest.raises(RuntimeError, match="Another automation synchronization"):
            await asyncio.wait_for(ctx.service.reconcile(row["id"]), 3)
        paused = await asyncio.wait_for(ctx.service.mutate(
            ctx.grant, "pause", row["id"],
            Mutation(revision=row["revision"], request_key="edit-during-remote-update"),
        ), 3)
        assert paused["outcome"] == "saved" and paused["sync_state"] == "pending"
    finally:
        ctx.remote.update_release.set()
        result = await asyncio.wait_for(syncing, 5)
    assert result["status"] == "ready"
    assert ctx.remote.schedules[native_schedule_id(row["id"], "default")].state.paused


@pytest.mark.parametrize("failure", ["remote_error", "cancellation"])
async def test_failed_reconcile_releases_lock_and_preserves_pending_intent(
    single_connection_automations, monkeypatch, failure,
):
    ctx = single_connection_automations
    row = await fixtures.create(ctx)
    await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="retry-failed-single-connection-sync", activate=True,
    ))
    entered, release = asyncio.Event(), asyncio.Event()

    async def unavailable(*args, **kwargs):
        entered.set()
        await release.wait()
        raise RPCError("scheduler unavailable", RPCStatusCode.UNAVAILABLE, b"")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.remote, "create_schedule", unavailable)
        syncing = asyncio.create_task(ctx.service.reconcile(row["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(RuntimeError, match="Another automation synchronization"):
                await asyncio.wait_for(ctx.service.reconcile(row["id"]), 3)
            if failure == "cancellation":
                syncing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await syncing
            else:
                release.set()
                with pytest.raises(RPCError):
                    await asyncio.wait_for(syncing, 3)
        finally:
            syncing.cancel()
            await asyncio.gather(syncing, return_exceptions=True)

    schedule_id = native_schedule_id(row["id"], "default")
    async with ctx.product.database.sessions() as db:
        binding = await db.get(Binding, row["id"])
        assert json.loads(binding.schedule_ids) == [schedule_id]
        assert binding.sync_state == "pending" and binding.synced_generation == 0
    assert not ctx.remote.schedules
    assert (await asyncio.wait_for(ctx.service.reconcile(row["id"]), 3))["status"] == "ready"
    assert not ctx.remote.schedules[schedule_id].state.paused
