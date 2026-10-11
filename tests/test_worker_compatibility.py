"""Worker release admission, revocation and readiness against real PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import threading
import time
from unittest.mock import AsyncMock

import pytest

from app import main, runtime_compatibility, schema_migrations
from app.database import DatabaseError, PostgresConnection, RUNTIME_SETTINGS, runtime_fingerprint
from app.temporal_runtime import TemporalRunManager
from test_api_compatibility import execute
from test_api_lifecycle import cluster, client_for  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401


def worker_settings(settings, **changes):
    return settings.model_copy(update={
        'moyai_runtime_role': 'worker', 'moyai_build_sha': 'b' * 40, **changes})


def policies(settings):
    return {table: execute(settings, 'SELECT * FROM ' + table)
            for table in ('runtime_policy', 'runtime_api_policy', 'runtime_worker_policy')}


def test_worker_contract_excludes_only_build_identity(split_settings):
    fingerprint = runtime_compatibility.worker_fingerprint(split_settings)
    for name in RUNTIME_SETTINGS:
        changed = split_settings.model_copy(update={name: 'synthetic-different-value'})
        assert (runtime_compatibility.worker_fingerprint(changed) == fingerprint) == (name == 'moyai_build_sha')
    assert fingerprint != runtime_compatibility.api_fingerprint(split_settings)


def test_compatible_workers_overlap_without_changing_coordinator_policy(cluster, monkeypatch):
    coordinator, settings = cluster
    before = policies(settings)
    original = PostgresConnection.execute

    def read_only(conn, sql, params=()):
        assert sql.strip().upper().startswith('SELECT'), sql
        return original(conn, sql, params)

    monkeypatch.setattr(PostgresConnection, 'execute', read_only)
    with ExitStack() as cleanup:
        for build in ('a' * 40, 'b' * 40, 'c' * 40):
            app = main.create_app(worker_settings(settings, moyai_build_sha=build))
            cleanup.callback(app.state.store.close)
            assert app.state.store.rows('SELECT 1 AS value') == [{'value': 1}]
            assert app.state.store.database.policy == runtime_fingerprint(coordinator.state.settings)
            assert app.state.store.database.worker_policy
            assert not app.state.store.database.broker_owner
        assert policies(settings) == before
        with pytest.raises(DatabaseError, match='Another coordinator'):
            main.create_app(worker_settings(settings, moyai_runtime_role='coordinator'))


@pytest.mark.parametrize('contract', ['WORKER_PROTOCOL_REVISION', 'BROKER_PROTOCOL_REVISION',
                                    'COORDINATOR_PROTOCOL_REVISION', 'API_PROTOCOL_REVISION', 'SCHEMA_REVISION'])
def test_incompatible_worker_contract_rejected_before_components(cluster, monkeypatch, contract):
    _, settings = cluster
    before = policies(settings)
    monkeypatch.setattr(runtime_compatibility, contract, 999)
    monkeypatch.setattr(main, '_create_app', lambda *args: pytest.fail('Rejected worker constructed components'))
    with pytest.raises(DatabaseError, match='Worker requires a compatible protocol'):
        main.create_app(worker_settings(settings))
    assert policies(settings) == before


@pytest.mark.parametrize('setting', [name for name in RUNTIME_SETTINGS if name not in {'moyai_build_sha', 'moyai_separate_broker'}])
def test_worker_shared_setting_drift_is_rejected(cluster, setting):
    _, settings = cluster
    with pytest.raises(DatabaseError, match='same shared runtime configuration'):
        main.create_app(worker_settings(settings, **{setting: 'synthetic-drift'}))


@pytest.mark.parametrize('mutation', [
    'DELETE FROM runtime_worker_policy',
    "UPDATE runtime_worker_policy SET fingerprint='incompatible'",
    "UPDATE runtime_worker_policy SET cluster_fingerprint='stale'",
    "UPDATE runtime_policy SET fingerprint='different-owner'",
])
def test_revoked_worker_cannot_read_write_or_claim_a_lease(cluster, mutation):
    _, settings = cluster
    app = main.create_app(worker_settings(settings))
    store = app.state.store
    try:
        execute(settings, mutation)
        for operation in (lambda: store.rows('SELECT 1'),
                          lambda: store.create_run('Rejected write', '', 'demo', []),
                          lambda: store.database.acquire_lease('session:fixture', 'fixture', 30)):
            with pytest.raises(DatabaseError, match='changed'):
                operation()
        assert execute(settings, 'SELECT count(*) FROM runs') == [(0,)]
        assert execute(settings, 'SELECT count(*) FROM runtime_leases') == [(0,)]
    finally:
        store.close()


def test_worker_requires_coordinator_publication_and_verified_schema(split_settings):
    schema_migrations.migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    with pytest.raises(DatabaseError, match='Worker requires a compatible protocol'):
        main.create_app(worker_settings(settings))
    coordinator = main.create_app(settings)
    try:
        execute(settings, 'UPDATE schema_state SET dirty=1')
        with pytest.raises(DatabaseError, match='incomplete, or incompatible'):
            main.create_app(worker_settings(settings))
    finally:
        coordinator.state.store.close()


@pytest.mark.parametrize('verify,split', [(False, True), (True, False)])
def test_legacy_or_unsplit_cluster_keeps_exact_build_admission(split_settings, verify, split):
    settings = split_settings.model_copy(update={'moyai_separate_broker': split})
    if verify:
        schema_migrations.migrate(settings)
        settings = settings.model_copy(update={'moyai_schema_mode': 'verify'})
    coordinator = main.create_app(settings)
    try:
        assert execute(settings, 'SELECT * FROM runtime_worker_policy') == []
        with pytest.raises(DatabaseError, match='same shared runtime configuration'):
            main.create_app(worker_settings(settings))
    finally:
        coordinator.state.store.close()


def test_worker_contract_does_not_authorize_coordinator_upgrade_or_migration(cluster):
    coordinator, settings = cluster
    worker = main.create_app(worker_settings(settings))
    coordinator.state.store.close()
    try:
        with pytest.raises(DatabaseError, match='Drain and stop'):
            main.create_app(worker_settings(settings, moyai_runtime_role='coordinator'))
        with pytest.raises(DatabaseError, match='Another Moyai instance'):
            schema_migrations.migrate(settings)
    finally:
        worker.state.store.close()


def test_revocation_waits_for_worker_write_then_fences_next_transaction(cluster):
    _, settings = cluster
    worker = main.create_app(worker_settings(settings))
    started = threading.Event()

    def revoke():
        started.set()
        execute(settings, 'DELETE FROM runtime_worker_policy')

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with worker.state.store.connect() as conn:
                conn.execute("UPDATE organization SET name='admitted worker write' WHERE id=1")
                future = pool.submit(revoke)
                assert started.wait(5)
                deadline = time.monotonic() + 5
                while not execute(settings, "SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND query='DELETE FROM runtime_worker_policy'"):
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                assert not future.done()
            future.result(timeout=5)
        assert execute(settings, 'SELECT name FROM organization WHERE id=1') == [('admitted worker write',)]
        with pytest.raises(DatabaseError, match='Worker compatibility changed'):
            worker.state.store.execute("UPDATE organization SET name='rejected' WHERE id=1")
    finally:
        worker.state.store.close()


def test_failed_worker_startup_releases_owner_and_stops_background_tasks(cluster, monkeypatch):
    _, settings = cluster

    async def unavailable(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', unavailable)
    app = main.create_app(worker_settings(settings, temporal_startup_timeout_seconds=1))
    with pytest.raises(RuntimeError, match='startup deadline'):
        with client_for(app):
            pytest.fail('Worker accepted traffic before Temporal connected')
    assert app.state.store.database.owner.closed
    assert app.state.manager.closing
    assert app.state.manager.dispatch_task.done()
    assert app.state.manager.prepared_task.done()


def test_initial_worker_polling_failure_never_reports_ready(cluster, monkeypatch):
    _, settings = cluster
    failed = AsyncMock()
    failed.run.side_effect = RuntimeError('synthetic worker startup failure')
    monkeypatch.setattr(TemporalRunManager, 'make_worker', lambda *args: failed)
    app = main.create_app(worker_settings(settings, temporal_startup_timeout_seconds=1))
    with pytest.raises(RuntimeError, match='startup deadline'):
        with client_for(app):
            pytest.fail('Failed poller reported ready')
    assert not app.state.manager.ready.is_set()
    assert app.state.store.database.owner.closed
