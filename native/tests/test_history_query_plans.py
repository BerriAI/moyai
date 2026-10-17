"""PostgreSQL read-plan and pooled-setting regressions; live-app proof is separate."""
from __future__ import annotations

import asyncio
import os

import pytest
import test_product_continuation as continuation_fixtures
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_product_continuation import ROOT

from moyai.product_history import prepare_history_query
from moyai.product_routes import ProductReadService, Reader

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
continuation = continuation_fixtures.continuation


@pytest.mark.parametrize("exit_mode", ["commit", "rollback", "cancel"])
async def test_history_jit_setting_never_leaks_to_next_pool_borrower(exit_mode):
    url = os.environ["MOYAI_TEST_POSTGRES"].replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    entered, release = asyncio.Event(), asyncio.Event()

    async def read():
        async with engine.connect() as db:
            await db.execute(text("SET jit = on"))
            await db.commit()
            await prepare_history_query(db)
            assert await db.scalar(text("SHOW jit")) == "off"
            entered.set()
            await release.wait()
            if exit_mode == "commit":
                await db.commit()
            # Ordinary exit and cancellation both roll back the read.

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if exit_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        async with engine.connect() as db:
            assert await db.scalar(text("SHOW jit")) == "on"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


async def recursive_history(product):
    # Exercise the canonical recursive-view shape, not the materialized relation
    # fixtures used by the older compatibility tests.
    async with product.source.begin() as db:
        await db.execute(text("DROP TABLE moyai.run_roots"))
        await db.execute(text("DROP TABLE moyai.run_ancestry"))
        await db.execute(text("""CREATE VIEW moyai.run_ancestry AS
            WITH RECURSIVE tree(ancestor_id,run_id) AS (
                SELECT id,id FROM moyai.runs UNION
                SELECT tree.ancestor_id,r.id FROM tree JOIN moyai.runs r ON r.parent_run_id=tree.run_id
            ) SELECT ancestor_id,run_id FROM tree"""))
        await db.execute(text("""CREATE VIEW moyai.run_roots AS
            SELECT a.run_id,r.id AS root_id FROM moyai.run_ancestry a
            JOIN moyai.runs r ON r.id=a.ancestor_id WHERE r.parent_run_id=''"""))
        rows = []
        for root in range(64):
            for depth in range(4):
                sid = f"{1000 + root * 4 + depth:032x}"
                rows.append({
                    "id": sid, "owner": product.actor.id,
                    "parent": f"{999 + root * 4 + depth:032x}" if depth else "",
                    "private": "someone-else" if root == 5 and depth == 1 else "",
                    "deleted": "2026-10-03" if root == 6 and depth == 1 else "",
                })
        # Rootless cycles and an orphan must not acquire a visible root.
        for sid, parent in ((8000, 8001), (8001, 8000), (8002, 9999)):
            rows.append({"id": f"{sid:032x}", "parent": f"{parent:032x}",
                         "owner": product.actor.id, "private": "", "deleted": ""})
        await db.execute(text("""INSERT INTO moyai.runs(
            id,owner_id,parent_run_id,private_owner_id,deleted_at,display_title)
            VALUES(:id,:owner,:parent,:private,:deleted,'History query regression')"""), rows)
        await db.execute(text("ANALYZE moyai.runs"))


async def test_recursive_history_shares_expansion_and_preserves_ancestor_privacy(continuation):
    product = continuation
    await recursive_history(product)
    history = product.runtime.product_history
    principal = product.principal
    visible = {ROOT}
    for root in range(64):
        for depth in range(4):
            if root in {5, 6} and depth:
                continue
            visible.add(f"{1000 + root * 4 + depth:032x}")
    assert await history.recheck(principal, sorted(visible)) == visible
    denied = [f"{1000 + root * 4 + depth:032x}" for root in (5, 6) for depth in (1, 2, 3)]
    denied += [f"{sid:032x}" for sid in (8000, 8001, 8002)]
    assert await history.recheck(principal, denied) == set()
    assert len((await history.sessions(principal, limit=100))["sessions"]) == 65

    # The actual plan must expand each canonical view only once. Repeating the
    # ancestry view for self/lineage/ancestor ACLs used to create extra recursive
    # unions and trigger expensive compilation on tiny result sets.
    async with history.read() as db:
        assert await db.scalar(text("SHOW jit")) == "off"
        plan = (await db.execute(text(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + history.access_cte() + "SELECT id FROM visible"
        ), history.params(principal))).scalar_one()[0]
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)
    assert plan.get("JIT") is None
    assert sum(n["Node Type"] == "Recursive Union" for n in nodes(plan["Plan"])) == 2


async def test_native_list_search_and_detail_disable_jit_with_recursive_continuations(continuation):
    from moyai.models import Session

    product = continuation
    await recursive_history(product)
    async with product.database.sessions.begin() as db:
        db.add(Session(id="native-history-plan", owner_id=product.actor.id,
                       title="Current history", model="test", visibility="private"))
    observed = []

    def inspect_setting(conn, cursor, statement, parameters, context, many):
        if "moyai_sessions.title" in statement:
            cursor.execute("SHOW jit")
            observed.append(cursor.fetchone()[0])

    engine = product.database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", inspect_setting)
    try:
        service = ProductReadService(product.runtime)
        reader = Reader(product.actor, product.principal)
        listing = await service.native_list(reader, limit=100, offset=0, scope="mine")
        assert [s["id"] for s in listing["sessions"]] == ["native-history-plan"]
        found = await service.native_search(reader, "Current history", limit=100, offset=0)
        assert [s["id"] for s in found["sessions"]] == ["native-history-plan"]
        detail = await service.detail(reader, "native-history-plan", "native")
        assert detail["session"]["id"] == "native-history-plan"
        assert len(observed) >= 3 and set(observed) == {"off"}
        async with product.database.sessions() as db:
            assert await db.scalar(text("SHOW jit")) == "on"
    finally:
        event.remove(engine, "before_cursor_execute", inspect_setting)
