"""Capacity admission stays bounded as retained session history grows."""
import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from app.database import PostgresConnection, SQLiteConnection
from app.durable_runner import DurableRunner, initialize_schema
from scripts.admission_benchmark import populate
from test_durable import durable


@pytest.mark.parametrize('fail_replacement', [False, True])
def test_capacity_index_upgrade_is_atomic_and_preserves_current_predicate(durable, monkeypatch, fail_replacement):
    manager, _, run_id = durable
    store = manager.store
    with store.connect() as conn:
        conn.begin_write()
        conn.execute('DROP INDEX idx_durable_sessions_occupied')
        conn.execute("CREATE INDEX idx_durable_sessions_occupied ON durable_sessions(run_id) WHERE run_id != ''")
    query = ("SELECT indexdef AS definition FROM pg_indexes WHERE schemaname=current_schema() AND indexname=?"
             if store.database else "SELECT sql AS definition FROM sqlite_schema WHERE type='index' AND name=?")
    before = store.rows(query, ('idx_durable_sessions_occupied',))
    connection_type = PostgresConnection if store.database else SQLiteConnection
    execute = connection_type.execute

    def replace(conn, sql, params=()):
        if fail_replacement and sql.startswith('CREATE INDEX idx_durable_sessions_occupied'):
            raise RuntimeError('Synthetic index creation failure')
        return execute(conn, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(connection_type, 'execute', replace)
        if fail_replacement:
            with pytest.raises(RuntimeError, match='Synthetic index creation failure'):
                initialize_schema(store)
            assert store.rows(query, ('idx_durable_sessions_occupied',)) == before
        else:
            initialize_schema(store)
    # Retrying a failed upgrade must install the new admission predicate too.
    initialize_schema(store)
    assert 'independent_runtime' in store.rows(query, ('idx_durable_sessions_occupied',))[0]['definition']
    manager.settings.max_concurrent_runs = 1
    manager.save(run_id, {'phase': 'monitor', 'independent_runtime': True, 'computer': {'phase': 'idle'}})
    assert manager.has_capacity()
    manager.save(run_id, {'phase': 'monitor', 'independent_runtime': True, 'computer': {'phase': 'launch'}})
    assert not manager.has_capacity()


@pytest.mark.parametrize('phase,occupied', [
    ('idle', False), ('waiting_children', False), ('waiting_credential', False),
    ('waiting_environment', False), ('prepare', True), ('provision', True),
    ('install', True), ('launch', True), ('monitor', True), ('save', True),
    ('checkpointed', True), ('finish', True), ('cleanup', True), ('warm', True),
    ('warm_cleanup', True), ('startup_wait', True), ('transport_wait', True),
    ('unknown-future-phase', True), (None, True),
])
def test_capacity_tracks_persisted_phase_transitions_and_restart(durable, phase, occupied):
    manager, _, run_id = durable
    manager.settings.max_concurrent_runs = 1
    assert manager.has_capacity()  # Default empty journal does not reserve a slot.
    manager.save(run_id, {'phase': phase})
    assert manager.has_capacity() is not occupied
    restarted = DurableRunner(manager.store, manager.settings)
    assert restarted.has_capacity() is not occupied
    restarted.save(run_id, {'phase': 'idle'})
    assert restarted.has_capacity()


@pytest.mark.parametrize('occupied', [99, 100])
async def test_admission_work_does_not_grow_with_retained_history(durable, occupied):
    manager, _, run_id = durable
    populate(manager.store, history=30000, occupied=occupied)
    connect = manager.store.connect
    steps = 0

    def budget():
        nonlocal steps
        steps += 1000
        # A deterministic DB work bound, independent of machine speed. Scanning
        # 30,000 finished sessions exceeds this; scanning active slots does not.
        return steps > 10000

    @contextmanager
    def bounded_connection():
        with connect() as conn:
            conn.set_progress_handler(budget, 1000)
            yield conn

    if manager.store.database:
        manager.store.execute('ANALYZE durable_sessions')
        rows = manager.store.rows

        def indexed_rows(sql, params=()):
            plan = rows('EXPLAIN (ANALYZE, FORMAT JSON) ' + sql, params)[0]['QUERY PLAN'][0]['Plan']

            def check(node):
                if node.get('Relation Name') == 'durable_sessions':
                    assert node['Node Type'] in {'Index Scan', 'Index Only Scan', 'Bitmap Heap Scan'}
                    assert node['Actual Rows'] + node.get('Rows Removed by Filter', 0) <= 100
                for child in node.get('Plans', []):
                    check(child)

            check(plan)
            return rows(sql, params)

        manager.store.rows = indexed_rows
    else:
        manager.store.connect = bounded_connection
    async with manager.admission(run_id) as available:
        assert available is (occupied < 100)
    assert len(manager.locks) <= 100


async def test_only_reclaimable_sessions_need_cleanup_locks(durable):
    manager, _, run_id = durable
    populate(manager.store, history=200, occupied=100)
    assert await manager.make_capacity(run_id) is False
    assert manager.locks == {}


async def test_failed_eviction_keeps_capacity_reserved_after_restart(durable):
    manager, _, run_id = durable
    manager.settings.max_concurrent_runs = 1
    state = {'phase': 'warm', 'sandbox_id': 'synthetic', 'idle_until': 1}
    manager.save(run_id, state)
    # A provider failure cannot advertise a free slot or let a new task start.
    manager.store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (run_id,))
    manager.cleanup = AsyncMock(side_effect=ConnectionError('Synthetic provider outage'))
    with pytest.raises(ConnectionError):
        await manager.make_capacity('new-session')
    assert manager.state(run_id)['phase'] == 'warm_cleanup'
    restarted = DurableRunner(manager.store, manager.settings)
    assert not restarted.has_capacity()


async def test_full_capacity_skips_queued_and_locked_warm_sessions(durable):
    manager, _, run_id = durable
    manager.settings.max_concurrent_runs = 2
    populate(manager.store, history=0, occupied=1)
    manager.save(run_id, {'phase': 'warm', 'idle_until': 1})
    manager.save('fixture-0', {'phase': 'warm', 'idle_until': 2})
    manager.release_warm = AsyncMock()
    async with manager.locks.setdefault('fixture-0', asyncio.Lock()):
        assert await manager.make_capacity('new-session') is False
    manager.release_warm.assert_not_awaited()
