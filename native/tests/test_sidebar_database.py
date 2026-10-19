"""Full sidebar equivalence and an opt-in, disposable PostgreSQL benchmark.

Run with MOYAI_BENCHMARK_SIDEBAR=1 and MOYAI_TEST_POSTGRES pointing at localhost.
Provider/network/browser latency is outside this service-level measurement.
"""
from __future__ import annotations

import json
import os
from statistics import median
from time import perf_counter
from types import MethodType
from typing import Any

import pytest
import test_product_continuation as fixtures
from sqlalchemy import bindparam, event, text
from test_history_query_plans import recursive_history

from moyai.models import Session
from moyai.product_continuation_schema import identifier
from moyai.product_history import SOURCE_FORMAT, Principal, bounded
from moyai.product_routes import ProductReadService, Reader

pytestmark = pytest.mark.skipif(not os.getenv('MOYAI_TEST_POSTGRES'), reason='Disposable local PostgreSQL required.')
continuation = fixtures.continuation


# Unmodified listing algorithm from a4600c79, retained only as the comparison baseline.
async def original_sessions(
    self, principal: Principal, *, limit: int = 50, offset: int = 0,
    archived: bool = False, deleted: bool = False, exclude_continued: bool = False,
    personal: bool = False,
) -> dict[str, Any]:
    bounded(limit, offset)
    current = await self.identity.require_current(principal.grant)
    t = self.table
    where = "r.parent_run_id='' AND "
    if deleted:
        where += "r.deleted_at!='' AND (:is_admin=1 OR r.owner_id=:actor)"
    else:
        where += self.navigation_scope(current, archived=archived, personal=personal)
        where += f" AND {' ' if archived else 'NOT '}EXISTS(SELECT 1 FROM {t('session_archives')} a " \
                 "WHERE a.owner_id=:actor AND a.run_id=r.id)"
    if exclude_continued and self.continuation_schema:
        where += f""" AND NOT EXISTS(SELECT 1 FROM "{self.continuation_schema}".moyai_product_continuations c
            WHERE c.source_schema=:continuation_source AND c.source_session_id=r.id)"""
    async with self.read() as conn:
        result = await conn.execute(text(
            self.access_cte(deleted=deleted) + f"SELECT {self.columns()} {self.from_runs()} WHERE {where} "
            "ORDER BY r.updated_at DESC,r.created_at DESC,r.id DESC LIMIT :limit OFFSET :offset"
        ), self.params(current) | {"limit": limit + 1, "offset": offset, "is_admin": int(current.role == "admin"),
                                  "continuation_source": self.schema})
        rows = [dict(r) for r in result.mappings()]
    valid = await self.recheck(current, [r["id"] for r in rows], deleted=deleted)
    return {"sessions": [self.view(r, current) for r in rows[:limit] if r["id"] in valid],
            "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
            "source_format": SOURCE_FORMAT}


async def original_allowed_sources(self, db, principal, ids: list[str]) -> set[str]:
    """The mapped native privacy/deletion boundary also protects old files."""
    if not ids:
        return set()
    query = text(f"""
        SELECT r.run_id FROM {self.table('run_roots')} r
        LEFT JOIN {identifier(self.native_schema)}.moyai_product_continuations c
          ON c.source_schema=:schema AND c.source_session_id=r.root_id
        LEFT JOIN {identifier(self.native_schema)}.moyai_sessions s ON s.id=c.native_session_id
        WHERE r.run_id IN :ids AND (c.source_session_id IS NULL OR
          (s.deleted=false AND (s.visibility='shared' OR (s.owner_id=:actor AND :individual))))
    """).bindparams(bindparam("ids", expanding=True))
    return set((await db.execute(query, {
        "schema": self.source_schema, "ids": ids, "actor": principal.id, "individual": principal.individual,
    })).scalars())


def original_graph_cte(self):
    t = self.table
    return f"""WITH ancestry AS MATERIALIZED (
        SELECT run_id,ancestor_id FROM {t('run_ancestry')}
    ), history_roots AS MATERIALIZED (
        SELECT run_id,root_id FROM {t('run_roots')}
    )"""


@pytest.mark.parametrize('deleted', [False, True])
async def test_sidebar_keeps_every_field_and_visibility_check(continuation, deleted, monkeypatch):
    product = continuation
    await recursive_history(product)
    async with product.source.begin() as db:
        await db.execute(text("UPDATE moyai.runs SET deletion_requested_at='2026-10-19' WHERE id=:id"),
                         {'id': f'{1000 + 8 * 4 + 1:032x}'})
    service, reader = ProductReadService(product.runtime), Reader(product.actor, product.principal)
    async with product.source.connect() as db:
        expected_graph = set((await db.execute(text('SELECT * FROM moyai.run_ancestry'))).all())
        views_before = (await db.execute(text(
            "SELECT viewname,definition FROM pg_views WHERE schemaname='moyai' ORDER BY viewname"))).all()
    with monkeypatch.context() as patch:
        patch.setattr(service.history, 'sessions', MethodType(original_sessions, service.history))
        patch.setattr(service.history, 'graph_cte', MethodType(original_graph_cte, service.history))
        patch.setattr(service.history, 'continuation_access', MethodType(original_allowed_sources, product.service))
        before = await service.listing(reader, source='all', scope='mine', limit=100, deleted=deleted)
    after = await service.listing(reader, source='all', scope='mine', limit=100, deleted=deleted)
    assert after == before
    assert not after['partial']
    # An old reader can still serve the same data after the new reader.
    with monkeypatch.context() as patch:
        patch.setattr(service.history, 'sessions', MethodType(original_sessions, service.history))
        patch.setattr(service.history, 'graph_cte', MethodType(original_graph_cte, service.history))
        patch.setattr(service.history, 'continuation_access', MethodType(original_allowed_sources, product.service))
        assert await service.listing(reader, source='all', scope='mine', limit=100, deleted=deleted) == before
    async with service.history.read() as db:
        actual_graph = set((await db.execute(text(
            service.history.graph_cte() + ' SELECT ancestor_id,run_id FROM ancestry'))).all())
        assert actual_graph == expected_graph
    async with product.source.connect() as db:
        assert set((await db.execute(text('SELECT * FROM moyai.run_ancestry'))).all()) == expected_graph
        assert (await db.execute(text(
            "SELECT viewname,definition FROM pg_views WHERE schemaname='moyai' ORDER BY viewname"))).all() == views_before
    # A previous successful read must not cache access across revocation.
    async with product.source.begin() as db:
        await db.execute(text('UPDATE moyai.runs SET private_owner_id=:other WHERE id=:id'),
                         {'other': 'someone-else', 'id': f'{1000:032x}'})
    assert f'{1000:032x}' not in {
        row['id'] for row in (await service.listing(reader, source='all', scope='mine', limit=100))['sessions']}


async def seed_sidebar(product, runs, depth):
    await recursive_history(product)
    async with product.source.begin() as db:
        # Match 43 legacy + 16 native results while retaining other users' data.
        await db.execute(text('UPDATE moyai.runs SET owner_id=:other WHERE id<>:root'),
                         {'other': 'another-owner', 'root': fixtures.ROOT})
        await db.execute(text('''INSERT INTO moyai.runs(id,owner_id,parent_run_id,display_title)
            SELECT 'bench-'||i,CASE WHEN i<:owned THEN :actor ELSE 'another-owner' END,
                CASE WHEN i % :depth=0 THEN '' ELSE 'bench-'||(i-1) END,'Sidebar benchmark'
            FROM generate_series(0,:last) AS i'''),
                         {'actor': product.actor.id, 'owned': 42 * depth, 'depth': depth, 'last': runs - 1})
        for query in (
            'CREATE INDEX idx_runs_parent ON moyai.runs(parent_run_id)',
            'CREATE INDEX idx_runs_owner ON moyai.runs(owner_id,id)',
            'CREATE INDEX idx_runs_parent_updated ON moyai.runs(parent_run_id,updated_at DESC,created_at DESC,id DESC)',
            "CREATE INDEX idx_messages_participant ON moyai.messages(user_id,run_id) WHERE role='user'",
            'ANALYZE moyai.runs',
        ):
            await db.execute(text(query))
    async with product.database.sessions.begin() as db:
        for i in range(16):
            db.add(Session(id=f'web-bench-{i}', owner_id=product.actor.id, title='Sidebar benchmark',
                           model='test', visibility='private'))


async def test_navigation_pages_keep_archive_scope_and_order(continuation, monkeypatch):
    product = continuation
    await recursive_history(product)
    history = product.runtime.product_history
    async with product.source.begin() as db:
        await db.execute(text("UPDATE moyai.runs SET owner_id=:other WHERE parent_run_id=''"),
                         {'other': 'another-owner'})
        await db.execute(text('INSERT INTO moyai.session_archives(owner_id,run_id) VALUES(:actor,:id)'),
                         {'actor': product.actor.id, 'id': f'{1000:032x}'})
        await db.execute(text("UPDATE moyai.runs SET deleted_at='2026-10-19',owner_id=:actor WHERE id=:id"),
                         {'actor': product.actor.id, 'id': f'{1004:032x}'})
    # Owned children still make their shared roots personal. Equal timestamps
    # exercise the ID tie-breaker across page boundaries.
    for options in ({'personal': True}, {'personal': False}, {'archived': True}, {'deleted': True}):
        for offset in (0, 3, 6, 63):
            with monkeypatch.context() as patch:
                patch.setattr(history, 'sessions', MethodType(original_sessions, history))
                patch.setattr(history, 'graph_cte', MethodType(original_graph_cte, history))
                patch.setattr(history, 'continuation_access', MethodType(original_allowed_sources, product.service))
                before = await history.sessions(product.principal, limit=3, offset=offset, **options)
            if offset == 0:
                assert before['sessions']
            assert await history.sessions(product.principal, limit=3, offset=offset, **options) == before


@pytest.mark.skipif(not os.getenv('MOYAI_BENCHMARK_SIDEBAR'), reason='Opt-in comparative benchmark.')
@pytest.mark.parametrize('runs,depth', [(4000, 4), (16000, 4), (4000, 16)])
async def test_sidebar_database_benchmark(continuation, runs, depth, monkeypatch):
    product = continuation
    await seed_sidebar(product, runs, depth)
    service, reader = ProductReadService(product.runtime), Reader(product.actor, product.principal)
    engine = product.source.sync_engine
    sql_times = []

    def before_query(conn, cursor, statement, parameters, context, many):
        context.sidebar_started = perf_counter()

    def after_query(conn, cursor, statement, parameters, context, many):
        if statement.startswith('WITH ancestry AS') or 'run_ancestry' in statement or 'run_roots' in statement:
            sql_times.append((perf_counter() - context.sidebar_started) * 1000)

    event.listen(engine, 'before_cursor_execute', before_query)
    event.listen(engine, 'after_cursor_execute', after_query)
    timings, expected = {}, None
    current_sessions, current_graph = service.history.sessions, service.history.graph_cte
    current_guard = service.history.continuation_access
    try:
        for name in ('before', 'after'):
            monkeypatch.setattr(service.history, 'continuation_access',
                                MethodType(original_allowed_sources, product.service) if name == 'before' else current_guard)
            monkeypatch.setattr(service.history, 'graph_cte',
                                MethodType(original_graph_cte, service.history) if name == 'before' else current_graph)
            monkeypatch.setattr(service.history, 'sessions',
                                MethodType(original_sessions, service.history) if name == 'before' else current_sessions)
            # Give each version the same warm-up and seven measured reads.
            await service.listing(reader, source='all', scope='mine', limit=100)
            samples, graph_samples = [], []
            for _ in range(7):
                sql_times.clear()
                started = perf_counter()
                result = await service.listing(reader, source='all', scope='mine', limit=100)
                samples.append((perf_counter() - started) * 1000)
                graph_samples.append(sum(sql_times))
                assert not result['partial'] and len(result['sessions']) == 59
                if expected is None:
                    expected = result
                assert result == expected
            timings[name] = {'sidebar_ms': round(median(samples), 2),
                             'history_sql_ms': round(median(graph_samples), 2),
                             'samples_ms': [round(value, 2) for value in samples]}
    finally:
        event.remove(engine, 'before_cursor_execute', before_query)
        event.remove(engine, 'after_cursor_execute', after_query)
    print('\nSIDEBAR_DATABASE ' + json.dumps({'runs': runs + 260, 'depth': depth, **timings}))


async def test_sparse_history_does_not_rescan_visible_tree_per_root(continuation):
    product = continuation
    await seed_sidebar(product, 800, 4)
    history = product.runtime.product_history
    queries = []

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith('WITH ancestry') and 'ORDER BY r.updated_at' in statement:
            queries.append((str(context.compiled.statement), context.compiled_parameters[0]))

    engine = product.source.sync_engine
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert len((await history.sessions(product.principal, limit=100, personal=True))['sessions']) == 43
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(queries) == 1
    statement, params = queries[0]
    async with history.read() as db:
        plan = (await db.execute(text('EXPLAIN (ANALYZE, FORMAT JSON) ' + statement), params)).scalar_one()[0]

    # Bound work by graph size, not number of roots times graph size. Include
    # filter rejections: the old plan returned few rows but scanned them all.
    def visible_scans(node):
        here = ((node['Actual Rows'] + node.get('Rows Removed by Filter', 0)) * node['Actual Loops']
                if node.get('CTE Name') == 'visible' else 0)
        return here + sum(visible_scans(child) for child in node.get('Plans', []))

    assert visible_scans(plan['Plan']) <= 4 * 1060
