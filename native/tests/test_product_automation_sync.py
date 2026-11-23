"""Synchronization failures through the application interface and real PostgreSQL."""

from __future__ import annotations

import asyncio
import copy

import pytest
import test_product_automations as fixtures
from temporalio.service import RPCError, RPCStatusCode

from moyai.automations.product_automations import (
    Migration,
    Mutation,
    Save,
    legacy_schedule_id,
    native_schedule_id,
)

pytestmark = fixtures.pytestmark
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations


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


async def test_remote_operation_lock_serializes_reconcilers_without_blocking_edits(automations):
    ctx = automations
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
