"""Real PostgreSQL read-only drain checks, including unknown/new phase rejection."""
import json
import os
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg
import pytest

from app.config import Settings
from app.main import create_app
from scripts.release.deploy import Release
from scripts.release.probe import POLICY_FIELDS, policy_fingerprint, snapshot


@pytest.fixture
def production_shape(tmp_path, request):
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_release_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    app = None
    try:
        settings = Settings(_env_file=None, data_dir=tmp_path, moyai_database_url=url,
            moyai_database_schema=schema, moyai_database_initialize=True, moyai_runtime_role='coordinator',
            temporal_enabled=True, object_storage_bucket='synthetic-release',
            session_secret='synthetic-release-key', encryption_key=Fernet.generate_key().decode(),
            session_titles_enabled=False, moyai_separate_broker=getattr(request, 'param', False))
        app = create_app(settings)
        yield app.state.store, url, schema, settings
    finally:
        if app: app.state.store.close()
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_probe_reports_real_ownership_and_enforces_read_only(production_shape):
    store, url, schema, _ = production_shape
    with psycopg.connect(url) as connection:
        result = snapshot(connection, schema)
        assert (result['owners'], result['coordinators'], result['brokers'], result['unsafe_sessions']) == (1, 1, 1, 0)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute('DELETE FROM runs')
    assert all(k not in result for k in ('sessions', 'prompts', 'credentials', 'database_url'))


def test_stop_after_worker_exit_can_wait_for_replacement(production_shape):
    store, url, schema, _ = production_shape
    run = store.create_run('Synthetic late Stop', '', 'demo', [], chat_enabled=True)
    store.execute('INSERT INTO durable_sessions(run_id,state,revision,delivered) VALUES(?,?,1,1)',
                  (run['id'], json.dumps({'phase': 'warm'})))
    with psycopg.connect(url, autocommit=True) as worker:
        worker.execute('SELECT pg_advisory_lock_shared(726940, %s::regnamespace::oid::int)', (schema,))
        with psycopg.connect(url) as connection:
            drained = {'ok': True, **snapshot(connection, schema)}
        assert Release.settled(drained, 2, drained=True)
        assert not Release.execution_stopped(drained)
    # Stop is accepted by the coordinator after the worker releases ownership.
    store.update_run(run['id'], status='stopping')
    with psycopg.connect(url) as connection:
        stopped = {'ok': True, **snapshot(connection, schema)}
    assert stopped['unsafe_sessions'] == 1
    assert not Release.settled(stopped, 1, drained=True)
    assert Release.execution_stopped(stopped)


@pytest.mark.parametrize('phase,unsafe', [('monitor', 1), ('save', 1), ('finish', 1),
    ('new_unknown_phase', 1), ('warm_cleanup', 1), ('waiting_credential', 0), ('warm', 0), ('prepare', 0)])
def test_active_and_unknown_phases_block_the_switch(production_shape, phase, unsafe):
    store, url, schema, _ = production_shape
    run = store.create_run('Synthetic read-only release check', '', 'demo', [], chat_enabled=True)
    store.execute('INSERT INTO durable_sessions(run_id,state,revision,delivered) VALUES(?,?,1,1)',
                  (run['id'], json.dumps({'phase': phase})))
    with psycopg.connect(url) as connection:
        result = snapshot(connection, schema)
    assert result['unsafe_sessions'] == unsafe
    store.update_run(run['id'], status='stopping')
    with psycopg.connect(url) as connection:
        assert snapshot(connection, schema)['unsafe_sessions'] == 1


@pytest.mark.parametrize('production_shape', [False, True], indirect=True)
def test_release_fingerprint_matches_actual_runtime_policy(production_shape):
    store, url, schema, settings = production_shape
    with psycopg.connect(url) as connection:
        state = snapshot(connection, schema)
    assert policy_fingerprint(settings) == state['policy']
    assert policy_fingerprint(settings.model_copy(update={'moyai_separate_broker': not settings.moyai_separate_broker})) != state['policy']
    assert policy_fingerprint(settings.model_copy(update={'encryption_key': 'different'})) != state['policy']


def test_first_upgrade_verifies_legacy_fingerprint_without_accepting_it_for_new_build():
    values = {key: getattr(Settings(_env_file=None), key) for key in POLICY_FIELDS}
    legacy = SimpleNamespace(**values)
    old_fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    assert policy_fingerprint(legacy) == old_fingerprint
    assert policy_fingerprint(SimpleNamespace(**values, moyai_separate_broker=False)) != old_fingerprint


@pytest.mark.parametrize('production_shape', [True], indirect=True)
def test_broker_lock_is_observed_until_its_actual_postgres_session_exits(production_shape):
    _, url, schema, _ = production_shape
    with psycopg.connect(url, autocommit=True) as broker:
        broker.execute('SELECT pg_advisory_lock_shared(726940, %s::regnamespace::oid::int)', (schema,))
        broker.execute('SELECT pg_advisory_lock(726944, %s::regnamespace::oid::int)', (schema,))
        with psycopg.connect(url) as connection:
            state = {'ok': True, **snapshot(connection, schema)}
        assert Release.execution_stopped(state, brokers=1)
        assert not Release.execution_stopped(state, brokers=0)
    with psycopg.connect(url) as connection:
        state = {'ok': True, **snapshot(connection, schema)}
    assert Release.execution_stopped(state, brokers=0)
