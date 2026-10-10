"""Mixed-build API admission and revocation against real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import psycopg
import pytest

from app import main, runtime_compatibility, schema_migrations
from app.database import DatabaseError, PostgresConnection, RUNTIME_SETTINGS, runtime_fingerprint
from test_api_lifecycle import cluster, client_for  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401


def execute(settings, sql):
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        result = conn.execute(sql)
        return result.fetchall() if result.description else []


def policies(settings):
    return (execute(settings, 'SELECT * FROM runtime_policy'), execute(settings, 'SELECT * FROM runtime_api_policy'))


def test_only_build_identity_is_excluded_from_shared_settings(split_settings):
    original = runtime_compatibility.api_fingerprint(split_settings)
    for name in RUNTIME_SETTINGS:
        changed = split_settings.model_copy(update={name: 'synthetic-different-value'})
        assert (runtime_compatibility.api_fingerprint(changed) == original) == (name == 'moyai_build_sha')


@pytest.mark.parametrize('mutation', [
    'DELETE FROM runtime_api_policy',
    "UPDATE runtime_api_policy SET fingerprint='incompatible'",
    "UPDATE runtime_api_policy SET cluster_fingerprint='stale'",
    "UPDATE runtime_policy SET fingerprint='different-owner'",
])
def test_contract_loss_fences_live_api_reads_writes_and_health(cluster, mutation):
    _, settings = cluster
    with client_for(main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))) as client:
        store = client.app.state.store
        assert client.get('/health').status_code == 200
        execute(settings, mutation)
        assert client.get('/health').status_code == 503
        with pytest.raises(DatabaseError, match='changed'):
            store.rows('SELECT 1')
        with pytest.raises(DatabaseError, match='changed'):
            store.create_run('Must not be written', '', 'demo', [])
        assert execute(settings, 'SELECT count(*) FROM runs') == [(0,)]


@pytest.mark.parametrize('contract', ['API_PROTOCOL_REVISION', 'SCHEMA_REVISION'])
def test_incompatible_code_contract_rejected_before_components(cluster, monkeypatch, contract):
    _, settings = cluster
    before = policies(settings)
    monkeypatch.setattr(runtime_compatibility, contract, 999)
    monkeypatch.setattr(main, '_create_app', lambda *args: pytest.fail('Rejected API constructed components'))
    with pytest.raises(DatabaseError, match='compatible protocol'):
        main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    assert policies(settings) == before


@pytest.mark.parametrize('mutation', [
    'UPDATE schema_state SET dirty=1', 'UPDATE schema_state SET revision=999',
])
def test_compatible_contract_cannot_bypass_schema_verification(cluster, mutation):
    _, settings = cluster
    execute(settings, mutation)
    with pytest.raises(DatabaseError, match='incomplete, or incompatible'):
        main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))


def test_api_start_requires_coordinator_publication_and_never_writes_it(split_settings, monkeypatch):
    schema_migrations.migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    api = settings.model_copy(update={'moyai_runtime_role': 'api', 'moyai_build_sha': 'b' * 40})
    with pytest.raises(DatabaseError, match='compatible protocol'):
        main.create_app(api)
    coordinator = main.create_app(settings)
    try:
        before = policies(settings)
        original = PostgresConnection.execute

        def read_only(conn, sql, params=()):
            assert sql.strip().upper().startswith('SELECT'), sql
            return original(conn, sql, params)

        monkeypatch.setattr(PostgresConnection, 'execute', read_only)
        app = main.create_app(api)
        try:
            assert app.state.store.database.policy == runtime_fingerprint(settings)
            assert app.state.store.database.policy != runtime_fingerprint(api)
            assert policies(settings) == before
        finally:
            app.state.store.close()
    finally:
        coordinator.state.store.close()


@pytest.mark.parametrize('role', ['worker', 'broker'])
def test_contract_does_not_admit_other_mixed_build_roles(cluster, role):
    _, settings = cluster
    before = policies(settings)
    with pytest.raises(DatabaseError, match='same shared runtime configuration'):
        main.create_app(settings.model_copy(update={'moyai_runtime_role': role, 'moyai_build_sha': 'b' * 40}))
    assert policies(settings) == before


def test_api_owner_still_blocks_coordinator_upgrade_and_migration(cluster):
    coordinator, settings = cluster
    api = main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    coordinator.state.store.close()
    try:
        with pytest.raises(DatabaseError, match='Drain and stop'):
            main.create_app(settings.model_copy(update={'moyai_runtime_role': 'coordinator', 'moyai_build_sha': 'b' * 40}))
        with pytest.raises(DatabaseError, match='Another Moyai instance'):
            schema_migrations.migrate(settings)
    finally:
        api.state.store.close()


@pytest.mark.parametrize('failure', ['construction', 'publication'])
def test_failed_coordinator_preserves_both_policies(cluster, monkeypatch, failure):
    coordinator, settings = cluster
    before = policies(settings)
    coordinator.state.store.close()
    if failure == 'construction':
        def fail(*args):
            raise RuntimeError('synthetic failure')
        monkeypatch.setattr(main, '_create_app', fail)
    else:
        original = PostgresConnection.execute

        def fail(conn, sql, params=()):
            result = original(conn, sql, params)
            if 'INSERT INTO runtime_api_policy' in sql:
                raise RuntimeError('synthetic failure')
            return result
        monkeypatch.setattr(PostgresConnection, 'execute', fail)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        main.create_app(settings.model_copy(update={'moyai_runtime_role': 'coordinator', 'moyai_build_sha': 'b' * 40}))
    assert policies(settings) == before


def test_revocation_waits_for_admitted_write_and_fences_next_one(cluster):
    _, settings = cluster
    app = main.create_app(settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    started = threading.Event()

    def revoke():
        started.set()
        execute(settings, 'DELETE FROM runtime_api_policy')

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.state.store.connect() as conn:
                conn.execute("UPDATE organization SET name='admitted write' WHERE id=1")
                pending = pool.submit(revoke)
                assert started.wait(5)
                # Prove the revoker is blocked on our row lock, not merely slow.
                deadline = time.monotonic() + 5
                while not execute(settings, "SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND query='DELETE FROM runtime_api_policy'"):
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                assert not pending.done()
            pending.result(timeout=5)
        assert execute(settings, 'SELECT name FROM organization WHERE id=1') == [('admitted write',)]
        with pytest.raises(DatabaseError, match='API compatibility changed'):
            app.state.store.execute("UPDATE organization SET name='not admitted' WHERE id=1")
    finally:
        app.state.store.close()


def test_revision_two_upgrade_preserves_saved_work_and_requires_publication(split_settings):
    schema_migrations.migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    coordinator = main.create_app(settings)
    run = coordinator.state.store.create_run('Saved on revision two', '', 'demo', [])
    coordinator.state.store.close()
    execute(settings, 'DROP TABLE runtime_api_policy')
    execute(settings, 'UPDATE schema_state SET revision=2')
    with pytest.raises(DatabaseError, match='API compatibility is unavailable'):
        main.create_app(settings.model_copy(update={'moyai_runtime_role': 'api'}))
    schema_migrations.migrate(settings)
    assert execute(settings, 'SELECT * FROM runtime_api_policy') == []
    coordinator = main.create_app(settings)
    try:
        api = main.create_app(settings.model_copy(update={'moyai_runtime_role': 'api', 'moyai_build_sha': 'b' * 40}))
        try:
            assert api.state.store.run(run['id'])['prompt'] == 'Saved on revision two'
        finally:
            api.state.store.close()
    finally:
        coordinator.state.store.close()


def test_unsplit_coordinator_does_not_publish_api_admission(cluster):
    coordinator, settings = cluster
    coordinator.state.store.close()
    replacement = main.create_app(settings.model_copy(update={
        'moyai_runtime_role': 'coordinator', 'moyai_separate_broker': False}))
    try:
        assert execute(settings, 'SELECT * FROM runtime_api_policy') == []
        with pytest.raises(DatabaseError, match='compatible protocol'):
            main.create_app(settings)
    finally:
        replacement.state.store.close()
