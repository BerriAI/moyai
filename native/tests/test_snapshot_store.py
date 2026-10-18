"""Real PostgreSQL snapshot transaction and authority regression checks."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace

import asyncpg
import pytest
import test_product_role_authority as authority_fixtures
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    PostgresStepStore,
    RunRecord,
    ToolEffectRecord,
)

from moyai.product_role_authority import OwnedHarnessPool, ProductOwnershipLost
from moyai.snapshot_store import SnapshotStepStore

role = authority_fixtures.role

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)


class CountingPool:
    def __init__(self, pool):
        self.pool = pool
        self.acquisitions = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        async with self.pool.acquire() as connection:
            yield connection


def snapshot(run="snapshot", *, parts=5):
    return ContinuableSnapshot(
        run_id=run, step_index=1, idempotency_key="0:1:complete",
        messages=[
            ModelRequest(parts=[UserPromptPart(f"{run} part {index}\n" + "sample " * 10_000)])
            for index in range(parts)
        ],
    )


async def counts(role):
    return [
        await role.admin.fetchval(f'SELECT count(*) FROM "{role.schema}".{table}')
        for table in ("step_persistence_media", "step_persistence_snapshots", "step_persistence_snapshot_keys")
    ]


async def test_snapshot_uses_one_owned_acquisition_and_native_round_trip(role, monkeypatch):
    pool = CountingPool(OwnedHarnessPool(role.pool, role.authority))
    old = PostgresStepStore(pool, max_snapshots_per_run=None)
    new = SnapshotStepStore(pool, owned=True)
    await old.save_snapshot(snapshot("warm-old"))
    await new.save_snapshot(snapshot("warm-new"))
    checks = []
    original = type(role.authority).check_asyncpg

    async def check(self, connection, *, final=False):
        checks.append(final)
        await original(self, connection, final=final)

    monkeypatch.setattr(type(role.authority), "check_asyncpg", check)
    pool.acquisitions = 0
    await old.save_snapshot(snapshot("old"))
    assert pool.acquisitions == 6
    assert checks == [False, True] * 6
    checks.clear()
    pool.acquisitions = 0
    expected = snapshot("new")
    await new.save_snapshot(expected)
    assert pool.acquisitions == 1
    assert checks == [False, True]
    # A fresh native reader, including a new process after restart, uses the
    # unchanged SDK schema and media format.
    reader = PostgresStepStore(pool, max_snapshots_per_run=None)
    restored = await reader.latest_snapshot(run_id="new")
    assert restored == expected
    await new.save_snapshot(expected)
    assert len(await reader.list_snapshots(run_id="new")) == 1
    interrupted = replace(expected, state="interrupted", idempotency_key="1:1:interrupted")
    await new.save_snapshot(interrupted)
    assert (await reader.latest_snapshot(run_id="new")).state == "complete"
    assert (await reader.latest_snapshot(run_id="new", include_interrupted=True)).state == "interrupted"


async def test_late_snapshot_failure_rolls_back_media_and_idempotency_key(role, monkeypatch):
    store = SnapshotStepStore(OwnedHarnessPool(role.pool, role.authority), owned=True)
    prune = store._prune_snapshots

    async def fail(*_):
        raise RuntimeError("after snapshot insert")

    monkeypatch.setattr(store, "_prune_snapshots", fail)
    expected = snapshot()
    with pytest.raises(RuntimeError, match="after snapshot insert"):
        await store.save_snapshot(expected)
    assert await counts(role) == [0, 0, 0]
    monkeypatch.setattr(store, "_prune_snapshots", prune)
    # Also proves that initial lazy schema preparation survives rollback.
    await store.save_snapshot(expected)
    assert await counts(role) == [5, 1, 1]
    assert await store.latest_snapshot(run_id=expected.run_id) == expected


async def test_commit_failure_rolls_back_and_retry_can_claim_snapshot_key(role):
    store = SnapshotStepStore(OwnedHarnessPool(role.pool, role.authority), owned=True)
    await store.get_run(run_id="schema-only")
    await store.snapshot_media.prepare()
    # A deferred constraint fails at the real outer COMMIT, after the final
    # ownership check and after all SDK media/snapshot savepoints have released.
    await role.admin.execute(f"""
        ALTER TABLE "{role.schema}".step_persistence_snapshot_keys
        ADD CONSTRAINT require_run FOREIGN KEY (run_id)
        REFERENCES "{role.schema}".step_persistence_runs(run_id)
        DEFERRABLE INITIALLY DEFERRED
    """)
    expected = snapshot()
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await store.save_snapshot(expected)
    assert await counts(role) == [0, 0, 0]
    await store.register_run(RunRecord(run_id=expected.run_id))
    await store.save_snapshot(expected)
    assert await counts(role) == [5, 1, 1]
    assert await store.latest_snapshot(run_id=expected.run_id) == expected


async def test_cancellation_rolls_back_snapshot_but_keeps_write_ahead_receipt(role, monkeypatch):
    store = SnapshotStepStore(OwnedHarnessPool(role.pool, role.authority), owned=True)
    receipt = ToolEffectRecord(
        run_id="snapshot", tool_call_id="external-action", tool_name="write", status="started",
    )
    await store.record_tool_effect(receipt)
    inserted = asyncio.Event()
    put = store.snapshot_media.put

    async def block(data, **kwargs):
        uri = await put(data, **kwargs)
        inserted.set()
        await asyncio.Event().wait()
        return uri

    monkeypatch.setattr(store.snapshot_media, "put", block)
    saving = asyncio.create_task(store.save_snapshot(snapshot()))
    await asyncio.wait_for(inserted.wait(), 5)
    saving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(saving, 5)
    assert await counts(role) == [0, 0, 0]
    assert await store.list_unresolved_tool_effects(run_id="snapshot") == [receipt]
    monkeypatch.setattr(store.snapshot_media, "put", put)
    await store.save_snapshot(snapshot())
    assert await counts(role) == [5, 1, 1]


@pytest.mark.parametrize("revoke", ["role", "owner"])
async def test_revocation_during_snapshot_prevents_commit(role, monkeypatch, revoke):
    store = SnapshotStepStore(OwnedHarnessPool(role.pool, role.authority), owned=True)
    prune = store._prune_snapshots

    async def revoke_after_insert(*args):
        await prune(*args)
        if revoke == "owner":
            await role.owner.close()
        else:
            await role.admin.execute(
                f'UPDATE "{role.schema}".runtime_api_policy SET fingerprint=$1', "c" * 64,
            )

    monkeypatch.setattr(store, "_prune_snapshots", revoke_after_insert)
    with pytest.raises(ProductOwnershipLost):
        await store.save_snapshot(snapshot())
    assert await counts(role) == [0, 0, 0]
    with pytest.raises(ProductOwnershipLost):
        await store.save_snapshot(snapshot("after-revocation"))


@pytest.mark.parametrize("owned", [False, True])
async def test_concurrent_snapshots_do_not_exhaust_one_connection_pool(role, owned):
    pool = await asyncpg.create_pool(
        os.environ["MOYAI_TEST_POSTGRES"], min_size=1, max_size=1,
        server_settings={"search_path": f'"{role.schema}",pg_catalog'},
    )
    try:
        store = SnapshotStepStore(
            OwnedHarnessPool(pool, role.authority) if owned else pool, owned=owned,
        )
        expected = [snapshot(f"concurrent-{index}") for index in range(4)]
        await asyncio.wait_for(
            asyncio.gather(*(store.save_snapshot(item) for item in expected)), 10,
        )
        assert await counts(role) == [20, 4, 4]
        for item in expected:
            assert await store.latest_snapshot(run_id=item.run_id) == item
    finally:
        await pool.close()


async def test_inherited_context_cannot_borrow_another_tasks_connection(role):
    store = SnapshotStepStore(OwnedHarnessPool(role.pool, role.authority), owned=True)

    async def acquire_in_child():
        async with store.snapshot_pool.acquire():
            pytest.fail("A child task must not borrow a live snapshot connection.")

    async with store.snapshot_pool.snapshot():
        with pytest.raises(RuntimeError, match="another task"):
            await asyncio.create_task(acquire_in_child())
    # Scope teardown restores ordinary acquisition.
    async with store.snapshot_pool.acquire() as connection:
        assert await connection.fetchval("SELECT 1") == 1
