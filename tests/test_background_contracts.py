"""Background contracts cannot silently admit a mutually incompatible role."""
import pytest

from app import main, runtime_compatibility
from app.database import DatabaseError, runtime_fingerprint
from test_api_compatibility import execute
from test_api_lifecycle import cluster  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401


@pytest.mark.parametrize('role', ['worker', 'broker'])
@pytest.mark.parametrize('revision', ['API_PROTOCOL_REVISION', 'WORKER_PROTOCOL_REVISION',
                                    'BROKER_PROTOCOL_REVISION', 'COORDINATOR_PROTOCOL_REVISION', 'SCHEMA_COMPATIBILITY_REVISION'])
def test_each_background_role_checks_all_reviewed_protocols_before_components(cluster, monkeypatch, role, revision):
    _, settings = cluster
    before = execute(settings, 'SELECT * FROM runtime_policy')
    monkeypatch.setattr(runtime_compatibility, revision, 999)
    monkeypatch.setattr(main, '_create_app', lambda *args: pytest.fail('Incompatible process constructed components'))
    with pytest.raises(DatabaseError, match='requires a compatible protocol'):
        main.create_app(settings.model_copy(update={'moyai_runtime_role': role, 'moyai_build_sha': 'b' * 40}))
    assert execute(settings, 'SELECT * FROM runtime_policy') == before


@pytest.mark.parametrize('role', ['worker', 'broker'])
@pytest.mark.parametrize('mutation', ['delete', 'stale', 'foreign-role'])
def test_role_contract_revocation_preserves_other_role_admission(cluster, role, mutation):
    coordinator, settings = cluster
    changed = settings.model_copy(update={'moyai_runtime_role': role, 'moyai_build_sha': 'b' * 40})
    app = main.create_app(changed)
    table = 'runtime_' + role + '_policy'
    try:
        if mutation == 'delete':
            execute(settings, f'DELETE FROM {table}')
        elif mutation == 'stale':
            execute(settings, f"UPDATE {table} SET cluster_fingerprint='stale'")
        else:
            other = 'broker' if role == 'worker' else 'worker'
            execute(settings, f'UPDATE {table} SET fingerprint=(SELECT fingerprint FROM runtime_{other}_policy WHERE id=1)')
        with pytest.raises(DatabaseError, match='compatibility changed'):
            app.state.store.rows('SELECT 1')
        with pytest.raises(DatabaseError, match='compatibility changed'):
            app.state.store.database.acquire_lease('session:fixture', 'fixture', 30)
        # API remains usable; a worker/broker contract cannot substitute for it.
        api = main.create_app(settings)
        try:
            assert api.state.store.rows('SELECT 1 AS value') == [{'value': 1}]
        finally:
            api.state.store.close()
        assert execute(settings, 'SELECT fingerprint FROM runtime_policy') == [(runtime_fingerprint(coordinator.state.settings),)]
    finally:
        app.state.store.close()


def test_failed_publication_rolls_back_every_background_contract(cluster, monkeypatch):
    from app.database import PostgresConnection
    coordinator, settings = cluster
    tables = ('runtime_policy', 'runtime_api_policy', 'runtime_worker_policy', 'runtime_broker_policy')
    before = {name: execute(settings, 'SELECT * FROM ' + name) for name in tables}
    coordinator.state.store.close()
    original = PostgresConnection.execute

    def failed_publish(conn, statement, params=()):
        result = original(conn, statement, params)
        if 'INSERT INTO runtime_broker_policy' in statement:
            raise RuntimeError('synthetic failed publication')
        return result

    monkeypatch.setattr(PostgresConnection, 'execute', failed_publish)
    with pytest.raises(RuntimeError, match='failed publication'):
        main.create_app(settings.model_copy(update={'moyai_runtime_role': 'coordinator', 'moyai_build_sha': 'b' * 40}))
    assert {name: execute(settings, 'SELECT * FROM ' + name) for name in tables} == before
