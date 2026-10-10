"""Real PostgreSQL read-only drain checks, including unknown/new phase rejection."""
import json
import os
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg
import pytest

from app.config import Settings
from app.main import create_app
from scripts.release.probe import snapshot


@pytest.fixture
def production_shape(tmp_path):
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
            session_titles_enabled=False)
        app = create_app(settings)
        yield app.state.store, url, schema
    finally:
        if app: app.state.store.close()
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_probe_reports_real_ownership_and_enforces_read_only(production_shape):
    store, url, schema = production_shape
    with psycopg.connect(url) as connection:
        result = snapshot(connection, schema)
        assert (result['owners'], result['coordinators'], result['unsafe_sessions']) == (1, 1, 0)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute('DELETE FROM runs')
    assert all(k not in result for k in ('sessions', 'prompts', 'credentials', 'database_url'))


@pytest.mark.parametrize('phase,unsafe', [('monitor', 1), ('save', 1), ('finish', 1),
    ('new_unknown_phase', 1), ('warm_cleanup', 1), ('waiting_credential', 0), ('warm', 0), ('prepare', 0)])
def test_active_and_unknown_phases_block_the_switch(production_shape, phase, unsafe):
    store, url, schema = production_shape
    run = store.create_run('Synthetic read-only release check', '', 'demo', [], chat_enabled=True)
    store.execute('INSERT INTO durable_sessions(run_id,state,revision,delivered) VALUES(?,?,1,1)',
                  (run['id'], json.dumps({'phase': phase})))
    with psycopg.connect(url) as connection:
        result = snapshot(connection, schema)
    assert result['unsafe_sessions'] == unsafe
    store.update_run(run['id'], status='stopping')
    with psycopg.connect(url) as connection:
        assert snapshot(connection, schema)['unsafe_sessions'] == 1
