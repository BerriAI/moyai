"""Real PostgreSQL migration exclusion, failed-upgrade recovery and no-DDL startup."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
import subprocess
import sys
import threading

import psycopg
import pytest

from app import main, schema_migrations as migration
from app.config import Settings
from app.database import DatabaseError, PostgresConnection
from app.db import Store
from app.trace_outbox import TABLES
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401
from test_runtime_startup_fences import schema_versions


def ready(settings):
    assert migration.migrate(settings) == {'status': 'ready', 'schema_revision': migration.SCHEMA_REVISION}
    return settings.model_copy(update={'moyai_schema_mode': 'verify'})


def execute(settings, sql):
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        return conn.execute(sql).fetchall()


def test_complete_offline_schema_and_no_runtime_constructor_writes(split_settings, monkeypatch):
    # Even disabled features must work later without an opportunistic migration.
    disabled = split_settings.model_copy(update={'temporal_enabled': False})
    ready(disabled)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    assert execute(settings, 'SELECT * FROM runtime_policy') == []
    with ExitStack() as cleanup:
        api = main.create_app(settings)
        cleanup.callback(api.state.store.close)
        store = api.state.store
        assert TABLES <= {
            r[0] for r in execute(settings, 'SELECT tablename FROM pg_tables WHERE schemaname=current_schema()')}
        # A legacy sentinel catches accidental backfills that contain no DDL.
        run = store.create_run('Synthetic saved run', '', 'modal', [])
        store.execute("UPDATE runs SET model='' WHERE id=?", (run['id'],))
        before = schema_versions(store)
        original = PostgresConnection.execute

        def read_only_construction(conn, sql, params=()):
            if not sql.strip().upper().startswith('SELECT') and not any(
                    table in sql for table in ('runtime_policy', 'runtime_api_policy')):
                pytest.fail('Unexpected runtime constructor write: ' + sql[:80])
            return original(conn, sql, params)

        with monkeypatch.context() as patch:
            patch.setattr(PostgresConnection, 'execute', read_only_construction)
            for role in ('worker', 'broker'):
                app = main.create_app(settings.model_copy(update={'moyai_runtime_role': role}))
                cleanup.callback(app.state.store.close)
            store.close()
            replacement = main.create_app(settings)
            cleanup.callback(replacement.state.store.close)
        store = replacement.state.store
        assert schema_versions(store) == before
        assert store.run(run['id'])['model'] == ''
        # Normal application writes still work in verify mode.
        assert store.create_run('A new ordinary session', '', 'modal', [])['id']
        with pytest.raises(DatabaseError, match='offline migration'):
            store.execute('CREATE TABLE accidental_startup (id INTEGER)')
        with pytest.raises(DatabaseError, match='offline migration'):
            with store.connect() as conn:
                conn.create_view('accidental_view', 'SELECT 1')


@pytest.mark.parametrize('role', ['standalone', 'coordinator', 'worker', 'broker', 'api'])
def test_migrator_excludes_every_runtime_owner(split_settings, role):
    settings = ready(split_settings)
    api = main.create_app(settings)
    app = api
    if role == 'standalone':
        api.state.store.close()
        app = main.create_app(settings.model_copy(update={'moyai_runtime_role': role, 'moyai_separate_broker': False}))
    elif role != 'coordinator':
        app = main.create_app(settings.model_copy(update={'moyai_runtime_role': role}))
        api.state.store.close()
    try:
        before = schema_versions(app.state.store)
        with pytest.raises(DatabaseError, match='Another Moyai instance'):
            migration.migrate(settings)
        assert schema_versions(app.state.store) == before
        assert execute(settings, 'SELECT dirty FROM schema_state') == [(0,)]
    finally:
        app.state.store.close()
        api.state.store.close()
    migration.migrate(settings)


def test_failed_migration_stays_dirty_and_retry_repairs_it(split_settings, monkeypatch):
    settings = ready(split_settings)
    original = migration.initialize_components

    def fail(store):
        store.execute('CREATE TABLE partial_upgrade (id INTEGER)')
        raise RuntimeError('synthetic interruption')

    monkeypatch.setattr(migration, 'initialize_components', fail)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        migration.migrate(settings)
    assert execute(settings, 'SELECT dirty FROM schema_state') == [(1,)]
    with pytest.raises(DatabaseError, match='incomplete'):
        main.create_app(settings)
    monkeypatch.setattr(migration, 'initialize_components', original)
    ready(settings)
    app = main.create_app(settings)
    app.state.store.close()


def test_failure_before_core_tables_can_be_retried(split_settings, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(Store, '_initialize_schema', lambda *a: (_ for _ in ()).throw(RuntimeError('core interrupted')))
        with pytest.raises(RuntimeError, match='core interrupted'):
            migration.migrate(split_settings)
    assert execute(split_settings, 'SELECT dirty FROM schema_state') == [(1,)]
    ready(split_settings)


def test_migrator_excludes_other_migrator_and_startup(split_settings, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    original = migration.initialize_components

    def hold(store):
        entered.set()
        assert finish.wait(10)
        original(store)

    monkeypatch.setattr(migration, 'initialize_components', hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        applying = pool.submit(migration.migrate, split_settings)
        assert entered.wait(10)
        try:
            with pytest.raises(DatabaseError, match='Another Moyai instance'):
                migration.migrate(split_settings)
            with pytest.raises(DatabaseError, match='Another Moyai instance'):
                main.create_app(split_settings.model_copy(update={'moyai_schema_mode': 'verify'}))
        finally:
            finish.set()
        assert applying.result(timeout=10)['status'] == 'ready'


@pytest.mark.parametrize('change', [
    'DELETE FROM schema_state', 'UPDATE schema_state SET dirty=1',
    'UPDATE schema_state SET revision=0', 'UPDATE schema_state SET revision=999',
    'DROP TABLE schema_state',
])
def test_wrong_schema_receipt_fails_before_runtime_setup(split_settings, change):
    settings = ready(split_settings)
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        before = conn.execute("SELECT oid,xmin::text FROM pg_proc WHERE pronamespace=current_schema()::regnamespace ORDER BY oid").fetchall()
        conn.execute(change)
    with pytest.raises(DatabaseError, match='incomplete'):
        main.create_app(settings)
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        assert conn.execute("SELECT oid,xmin::text FROM pg_proc WHERE pronamespace=current_schema()::regnamespace ORDER BY oid").fetchall() == before


def test_newer_schema_refuses_downgrade_and_auto_mode(split_settings):
    ready(split_settings)
    with pytest.raises(DatabaseError, match='MOYAI_SCHEMA_MODE=verify'):
        main.create_app(split_settings)
    execute(split_settings, 'UPDATE schema_state SET revision=999 RETURNING revision')
    with pytest.raises(DatabaseError, match='newer'):
        migration.migrate(split_settings)
    assert execute(split_settings, 'SELECT revision,dirty FROM schema_state') == [(999, 0)]


def test_cli_does_not_import_main_or_start_network_clients(split_settings):
    # Run the actual operator entry point in a fresh interpreter. Distributed
    # configuration would try runtime recovery if it imported app.main.
    env = {**os.environ, **{k.upper(): str(v) for k, v in split_settings.model_dump(mode='json').items()
                          if isinstance(v, (str, int, float, bool))}, 'MOYAI_SCHEMA_MODE': 'verify'}
    result = subprocess.run([sys.executable, '-m', 'app.schema_migrations', '--apply'],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'ready'
    assert execute(split_settings, 'SELECT * FROM runtime_policy') == []
    assert execute(split_settings, 'SELECT * FROM durable_sessions') == []


def test_verify_requires_postgres(tmp_path):
    with pytest.raises(ValueError, match='PostgreSQL'):
        Settings(_env_file=None, moyai_schema_mode='verify')
    with pytest.raises(ValueError, match='PostgreSQL'):
        Store(tmp_path, schema_mode='verify')


@pytest.mark.sqlite_only
def test_sqlite_login_state_upgrade_preserves_pending_login(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / 'workspace.db') as conn:
        conn.execute('''CREATE TABLE login_states (
            state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
            nonce TEXT NOT NULL, verifier TEXT NOT NULL,
            return_path TEXT NOT NULL, expires REAL NOT NULL)''')
        conn.execute("INSERT INTO login_states VALUES('state','browser','nonce','verifier','/',123)")
    for expected_client in ('', 'browser-client'):
        store = Store(tmp_path)
        try:
            assert store.rows('SELECT client,nonce,expires FROM login_states') == [
                {'client': expected_client, 'nonce': 'nonce', 'expires': 123}]
            store.execute("UPDATE login_states SET client='browser-client' WHERE state_hash='state'")
            assert store.rows("SELECT state_hash FROM login_states WHERE client='browser-client' AND expires>0") == [
                {'state_hash': 'state'}]
        finally:
            store.close()


def test_postgres_login_state_upgrade_requires_migration_and_preserves_login(split_settings):
    settings = ready(split_settings)
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        conn.execute('DROP INDEX login_states_client')
        conn.execute('ALTER TABLE login_states DROP COLUMN client')
        conn.execute("INSERT INTO login_states VALUES('state','browser','nonce','verifier','/',123)")
        conn.execute('UPDATE schema_state SET revision=3')
    with pytest.raises(DatabaseError, match='offline migration'):
        main.create_app(settings)
    ready(split_settings)
    assert execute(settings, 'SELECT client,nonce,expires FROM login_states') == [('', 'nonce', 123)]
    assert execute(settings, "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND indexname='login_states_client'") == [
        ('login_states_client',)]
    api = main.create_app(settings)
    try:
        api.state.store.execute("UPDATE login_states SET client='browser-client' WHERE state_hash='state'")
        assert api.state.store.rows("SELECT state_hash FROM login_states WHERE client='browser-client' AND expires>0") == [
            {'state_hash': 'state'}]
    finally:
        api.state.store.close()


def test_schema_initializers_have_a_versioned_manifest():
    # A schema edit without a new revision must fail CI, including optional
    # components and backend-specific functions/triggers used by the migrator.
    import ast
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sources = {}
    for path in (root / 'app').glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {'initialize_schema', '_initialize_schema', 'initialize_defaults'}:
                sources[f'{path.name}:{node.name}'] = ast.dump(node, include_attributes=False)
    sources['database_schema'] = ast.dump(ast.parse((root / 'app/database_schema.py').read_text()), include_attributes=False)
    sources['components'] = list(migration.COMPONENTS)
    sources['trace_tables'] = sorted(TABLES)
    assert {k.split(':')[0][:-3] for k in sources if k.endswith(':initialize_schema')} == (
        set(migration.COMPONENTS) | {'attachments', 'slack_mentions', 'blob_storage', 'trace_outbox'})
    signature = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    versions = json.loads((root / 'app/schema_versions.json').read_text())
    assert list(versions) == [str(i) for i in range(1, migration.SCHEMA_REVISION + 1)]
    assert versions[str(migration.SCHEMA_REVISION)] == signature, (
        'Schema changed. Increment SCHEMA_REVISION and append its source digest to app/schema_versions.json: ' + signature)


def test_opt_in_preserves_existing_data_keys_and_runtime_policy(split_settings):
    api = main.create_app(split_settings)
    store = api.state.store
    run = store.create_run('Existing saved session', '', 'modal', [])
    request = api.state.spend.begin(run, 'synthetic-model')
    preserved = {
        name: store.rows('SELECT * FROM ' + name)
        for name in ('runs', 'model_requests', 'sandbox_settings', 'organization', 'runtime_policy')
    }
    store.close()
    settings = ready(split_settings)
    replacement = main.create_app(settings)
    try:
        store = replacement.state.store
        assert {name: store.rows('SELECT * FROM ' + name) for name in preserved} == preserved
        assert store.rows('SELECT status FROM model_requests WHERE id=?', (request,))[0]['status'] == 'pending'
    finally:
        replacement.state.store.close()


def test_revision_one_upgrades_without_changing_saved_sessions(split_settings):
    # Reproduce revision 1's receipt and schema: the only new table is the
    # durable title inbox and deletion scan index. Existing state survives.
    settings = ready(split_settings)
    app = main.create_app(settings)
    run = app.state.store.create_run('Saved before API split', '', 'demo', [], chat_enabled=True)
    before = {name: app.state.store.rows('SELECT * FROM ' + name)
              for name in ('runs', 'messages', 'sandbox_settings', 'runtime_policy')}
    app.state.store.close()
    with psycopg.connect(settings.moyai_database_url) as conn:
        conn.execute(f'SET search_path TO "{settings.moyai_database_schema}"')
        conn.execute('DROP TABLE session_title_requests')
        conn.execute('DROP INDEX idx_runs_pending_deletion')
        conn.execute('UPDATE schema_state SET revision=1')
    with pytest.raises(DatabaseError, match='incompatible'):
        main.create_app(settings)
    ready(settings)
    app = main.create_app(settings)
    try:
        assert {name: app.state.store.rows('SELECT * FROM ' + name) for name in before} == before
        assert app.state.store.rows('SELECT * FROM session_title_requests') == []
        assert execute(settings, "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND indexname='idx_runs_pending_deletion'")
        assert app.state.store.run(run['id'])['prompt'] == 'Saved before API split'
    finally:
        app.state.store.close()
