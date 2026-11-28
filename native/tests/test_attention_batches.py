"""Bounded admission on real PostgreSQL; provider and schedule I/O are substituted."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import test_connection_attention as connection_fixtures
import test_google_attention as enrollment
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from sqlalchemy import func, select

from moyai.automations.product_automation_context_models import (
    ProductAutomationExecutionContext as ExecutionContext,
)
from moyai.automations.product_automation_models import ProductAutomationBinding as Binding
from moyai.automations.product_automation_models import ProductAutomationOccurrence as Occurrence
from moyai.automations.product_automations import Mutation, ProductAutomationService, Save
from moyai.database import grant_fingerprint
from moyai.dot_contracts import DotResearch
from moyai.dot_discovery import DiscoverySnapshot
from moyai.dot_models import DotAttentionTurn, DotDiscoveryTurn
from moyai.dot_sources import GITHUB_MCP
from moyai.models import Connection, Turn

pytestmark = connection_fixtures.pytestmark
admitted_product = connection_fixtures.admitted_product
automations = connection_fixtures.automations
canonical_connections = connection_fixtures.canonical_connections
google = connection_fixtures.google
connected = connection_fixtures.connected
remote_reads = connection_fixtures.remote_reads


async def enroll(ctx, count, **policy):
    ids = []
    identity = None
    for _ in range(count):
        account = await connection_fixtures.native_connect(ctx, "mcp", **policy)
        ids.append(account["id"])
        row, definition, binding, context = await enrollment.saved(ctx)
        identity = identity or row["id"]
        assert row["id"] == identity
        assert binding.state == "active" and not binding.last_error
        assert definition["connection_ids"] == sorted(ids)
        assert json.loads(context.connection_ids) == sorted(ids)
        await ctx.service.reconcile(identity)
        assert len(ctx.remote.schedules) == 1
        assert not next(iter(ctx.remote.schedules.values())).state.paused
    return sorted(ids)


async def scheduled_args(ctx, *, stamp=None):
    row, definition, binding, _ = await enrollment.saved(ctx)
    stamp = stamp or datetime.now(UTC)
    trigger = definition["triggers"][0]["id"]
    occurrence = f"native:{row['id']}:{trigger}:{stamp.isoformat()}"
    return row["id"], binding.generation, row["revision"], trigger, occurrence, stamp.isoformat()


async def admit(ctx, mode, index):
    if mode == "scheduled":
        return await ctx.service.admit(*await scheduled_args(ctx))
    row, _, _, _ = await enrollment.saved(ctx)
    return await ctx.service.run_now(ctx.grant, row["id"], Mutation(
        revision=row["revision"], request_key=f"manual-batch-{index}",
    ))


async def prepare(ctx, receipt):
    assert receipt["status"] == "admitted", receipt
    await ctx.product.database.claim_batch(20)
    return await ctx.product.database.admission.prepare(receipt["turn_id"])


@pytest.mark.parametrize("count", [5, 9])
@pytest.mark.parametrize("mode", ["scheduled", "manual"])
async def test_more_than_four_accounts_remain_active_and_rotate_persisted_batches(connected, remote_reads, count, mode):
    ctx = connected
    pool = await enroll(ctx, count)
    _, _, before, context = await enrollment.saved(ctx)
    chunks = [pool[start:start + 4] for start in range(0, count, 4)]
    observed = []
    for index in range(2 * len(chunks)):
        receipt = await admit(ctx, mode, index)
        prepared = await prepare(ctx, receipt)
        selected = sorted(grant.id for grant in prepared.scope.grants)
        assert selected == chunks[index % len(chunks)]
        assert len(selected) <= 4
        async with ctx.product.database.sessions() as db:
            turn = await db.get(Turn, receipt["turn_id"])
            attention = await db.get(DotAttentionTurn, turn.id)
            assert json.loads(turn.connections) == selected
            assert attention.grants_key == grant_fingerprint(prepared.scope.grants)
            assert attention.grants_key != json.loads(context.workspace_context)["grants_key"]
        observed.extend(selected)
        # A failed admitted attempt must not monopolize the first batch.
        await ctx.product.database.fail(receipt["turn_id"], "Synthetic failed check")
    assert all(observed.count(account) == 2 for account in pool)
    row, _, after, current_context = await enrollment.saved(ctx)
    assert after.state == "active" and after.generation == before.generation
    assert current_context.fingerprint == context.fingerprint
    assert len(ctx.remote.schedules) == 1
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Binding)) == 1
        assert await db.scalar(select(func.count()).select_from(Occurrence).where(
            Occurrence.automation_id == row["id"], Occurrence.turn_id.is_not(None),
        )) == 2 * len(chunks)


@pytest.mark.parametrize("mode", ["scheduled", "manual"])
async def test_duplicate_admission_and_service_restart_preserve_selection(connected, remote_reads, mode):
    ctx = connected
    pool = await enroll(ctx, 5)
    args = await scheduled_args(ctx)
    body = Mutation(revision=args[2], request_key="same-manual-batch")

    async def invoke():
        if mode == "scheduled":
            return await ctx.service.admit(*args)
        return await ctx.service.run_now(ctx.grant, args[0], body)

    first, retry = await asyncio.gather(invoke(), invoke())
    assert retry == first
    prepared = await prepare(ctx, first)
    assert sorted(grant.id for grant in prepared.scope.grants) == pool[:4]
    await ctx.product.database.fail(first["turn_id"], "Synthetic failed attempt")
    old = ctx.service
    ctx.service = ProductAutomationService(
        old.database, old.identity, old.settings, old.connect, connections=old.connections,
    )
    assert await invoke() == first
    second = await admit(ctx, mode, 2)
    prepared = await prepare(ctx, second)
    assert [grant.id for grant in prepared.scope.grants] == pool[4:]
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Occurrence)) == 2


async def test_overlap_and_expired_skips_do_not_consume_batches(connected, remote_reads):
    ctx = connected
    pool = await enroll(ctx, 9)
    expired = await ctx.service.admit(*await scheduled_args(ctx, stamp=datetime.now(UTC) - timedelta(minutes=16)))
    assert expired["status"] == "skipped"
    first = await admit(ctx, "scheduled", 0)
    assert sorted(grant.id for grant in (await prepare(ctx, first)).scope.grants) == pool[:4]
    skipped_args = await scheduled_args(ctx)
    skipped = await ctx.service.admit(*skipped_args)
    assert skipped["status"] == "skipped"
    await ctx.product.database.fail(first["turn_id"], "Synthetic failed check")
    assert await ctx.service.admit(*skipped_args) == skipped
    second = await admit(ctx, "scheduled", 1)
    assert sorted(grant.id for grant in (await prepare(ctx, second)).scope.grants) == pool[4:8]


async def test_competing_occurrences_admit_only_one_batch(connected, remote_reads):
    ctx = connected
    pool = await enroll(ctx, 5)
    first_args, second_args = await scheduled_args(ctx), await scheduled_args(ctx)
    receipts = await asyncio.gather(
        ctx.service.admit(*first_args), ctx.service.admit(*second_args),
    )
    assert sorted(receipt["status"] for receipt in receipts) == ["admitted", "skipped"]
    admitted = next(receipt for receipt in receipts if receipt["status"] == "admitted")
    assert sorted(grant.id for grant in (await prepare(ctx, admitted)).scope.grants) == pool[:4]
    await ctx.product.database.fail(admitted["turn_id"], "Synthetic failed batch")
    second = await prepare(ctx, await admit(ctx, "scheduled", 2))
    assert [grant.id for grant in second.scope.grants] == pool[4:]


async def test_failed_admission_transaction_does_not_consume_a_batch(connected, remote_reads, monkeypatch):
    ctx = connected
    pool = await enroll(ctx, 5)
    original = ctx.product.database.dots.bind_attention

    async def fail_after_turn(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("Synthetic failure before occurrence commit")

    args = await scheduled_args(ctx)
    with monkeypatch.context() as patch:
        patch.setattr(ctx.product.database.dots, "bind_attention", fail_after_turn)
        with pytest.raises(RuntimeError, match="before occurrence commit"):
            await ctx.service.admit(*args)
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Occurrence)) == 0
        assert await db.scalar(select(func.count()).select_from(DotAttentionTurn)) == 0
    receipt = await ctx.service.admit(*args)
    assert sorted(grant.id for grant in (await prepare(ctx, receipt)).scope.grants) == pool[:4]


@pytest.mark.parametrize("change", ["disabled", "revision"])
async def test_deferred_native_grant_changes_revoke_running_batch(connected, remote_reads, change):
    ctx = connected
    pool = await enroll(ctx, 5)
    prepared = await prepare(ctx, await admit(ctx, "scheduled", 0))
    deferred = pool[-1]
    assert deferred not in {grant.id for grant in prepared.scope.grants}
    # A real authority change without the enrollment callback must still fence
    # the old run through the full immutable context, not just selected grants.
    async with ctx.product.database.sessions.begin() as db:
        await ctx.service._writer(db)
        account = await db.get(Connection, deferred)
        account.revision += 1
        if change == "disabled":
            account.enabled = False
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.product.database.authorize_scope(prepared.scope)
    blocked = await admit(ctx, "scheduled", 1)
    assert blocked["status"] in {"skipped", "blocked"}
    await ctx.product.database.fail(prepared.scope.turn_id, "Synthetic retired check")
    assert (await admit(ctx, "scheduled", 2))["status"] == "blocked"


@pytest.mark.parametrize("change", ["missing", "fingerprint"])
async def test_missing_or_replaced_full_context_cannot_authorize_subset(connected, remote_reads, change):
    ctx = connected
    await enroll(ctx, 5)
    prepared = await prepare(ctx, await admit(ctx, "scheduled", 0))
    row, _, binding, _ = await enrollment.saved(ctx)
    async with ctx.product.database.sessions.begin() as db:
        await ctx.service._writer(db)
        captured = await db.get(ExecutionContext, (row["id"], binding.generation))
        replacement = {
            column.name: getattr(captured, column.name)
            for column in ExecutionContext.__table__.columns
        }
        # Existing SQL immutability rejects UPDATE. Simulate an absent/replaced
        # capture without disabling that trigger in this isolated database.
        await db.delete(captured)
        await db.flush()
        if change == "fingerprint":
            db.add(ExecutionContext(**{**replacement, "fingerprint": "0" * 64}))
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.product.database.authorize_scope(prepared.scope)


async def test_deferred_slack_opt_out_fences_old_batch_and_keeps_remaining_accounts_active(connected, remote_reads):
    ctx = connected
    await enroll(ctx, 4)
    slack = await connection_fixtures.personal_slack(ctx)
    enabled = await ctx.client.patch(f"/api/connections/{slack.id}/attention", json={
        "revision": slack.revision, "consent_revision": 0, "use_proactively": True,
    })
    assert enabled.status_code == 200, enabled.text
    row, _, _, _ = await enrollment.saved(ctx)
    await ctx.service.reconcile(row["id"])
    prepared = await prepare(ctx, await admit(ctx, "scheduled", 0))
    if slack.id in {grant.id for grant in prepared.scope.grants}:
        await ctx.product.database.fail(prepared.scope.turn_id, "Synthetic prior batch")
        prepared = await prepare(ctx, await admit(ctx, "scheduled", 1))
    assert slack.id not in {grant.id for grant in prepared.scope.grants}
    disabled = await ctx.client.patch(f"/api/connections/{slack.id}/attention", json={
        "revision": slack.revision, "use_proactively": False,
        "consent_revision": enabled.json()["attention"]["consent_revision"],
    })
    assert disabled.status_code == 200, disabled.text
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.product.database.authorize_scope(prepared.scope)
    row, definition, binding, _ = await enrollment.saved(ctx)
    assert slack.id not in definition["connection_ids"] and len(definition["connection_ids"]) == 4
    assert binding.state == "active"
    await ctx.service.reconcile(row["id"])
    assert len(ctx.remote.schedules) == 1
    assert not next(iter(ctx.remote.schedules.values())).state.paused


async def test_deferred_accounts_prevent_complete_coverage(connected, remote_reads):
    ctx = connected
    await enroll(ctx, 5)
    prepared = await prepare(ctx, await admit(ctx, "scheduled", 0))
    prompt = json.loads(prepared.prompt)
    assert prompt["deferred_account_count"] == 1
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[
            ToolReturnPart(
                tool_name=f"mcp_{grant.id[:8]}_read_item", tool_call_id=grant.id,
                content={"dot_scan": True, "scanned": True, "limited": False, "error": False, "records": []},
            )
            for grant in prepared.scope.grants
        ]),
    ])).decode()
    snapshot = DiscoverySnapshot.model_validate_json(await ctx.product.database.dots.discovery.judgment_input(
        prepared.scope, DotResearch(coverage="complete", observations=[]).model_dump_json(), conversation,
    ))
    assert snapshot.research.coverage == "partial"
    assert set(snapshot.accounts) == {grant.id for grant in prepared.scope.grants}


async def test_restricted_starting_locations_follow_the_selected_batch(connected, remote_reads):
    ctx = connected
    remote_reads.reads = ["list_issues"]
    pool = await enroll(ctx, 5, mcp={"url": GITHUB_MCP})
    row, definition, _, _ = await enrollment.saved(ctx)
    sources = [{"connection_id": pool[-1], "source": "https://github.com/synthetic/review"}]
    changed = await ctx.service.mutate(ctx.grant, "update", row["id"], Save(
        revision=row["revision"], request_key="select-restricted-source",
        definition=enrollment.automation_fixtures.definition(**{**definition, "attention_sources": sources}),
    ))
    await ctx.service.mutate(ctx.grant, "enable", row["id"], Mutation(
        revision=changed["revision"], request_key="enable-restricted-source",
    ))
    await ctx.service.reconcile(row["id"])
    for index, expected in enumerate(([], sources)):
        receipt = await admit(ctx, "scheduled", index)
        prepared = await prepare(ctx, receipt)
        async with ctx.product.database.sessions() as db:
            discovery = await db.get(DotDiscoveryTurn, receipt["turn_id"])
            assert json.loads(ctx.product.database.security.unseal(discovery.sources)) == expected
        assert json.loads(prepared.prompt)["starting_locations"] == expected
        await ctx.product.database.fail(receipt["turn_id"], "Synthetic completed batch")


def test_discovery_retains_thirty_account_enrollment_bound():
    definition = enrollment.automation_fixtures.definition(
        kind="attention", attention_mode="discovery", visibility="private",
        session_id=str(UUID(int=100)), connection_ids=[str(UUID(int=index + 1)) for index in range(30)],
    )
    assert len(definition.connection_ids) == 30
    with pytest.raises(ValidationError):
        definition.model_validate({
            **definition.model_dump(), "connection_ids": [str(UUID(int=index + 1)) for index in range(31)],
        })
