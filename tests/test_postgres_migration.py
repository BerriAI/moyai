"""Real Postgres tests for the offline migration, including rollback and fidelity."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sqlite3
from uuid import uuid4

import pytest

psycopg = pytest.importorskip('psycopg')
pytest.importorskip('sqlglot')

from app import postgres_migration as migration
from app.config import Settings
from app.main import create_app


@pytest.fixture
def source(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, session_titles_enabled=False))
    store = app.state.store
    run = store.create_run('Keep my history — 日本語 🗿', '', 'demo', [], chat_enabled=True)
    store.attachments.save(uuid4().hex, 'owner', 'binary.bin', bytes(range(256)),
                           ('application/octet-stream', b'', ''), 1024)
    encrypted = app.state.security.fernet.encrypt(b'synthetic-provider-token').decode()
    store.execute('INSERT INTO connections VALUES(?,?,?,?)', ('github', encrypted, 'Synthetic connection', '2026-10-09'))
    store.execute('CREATE TABLE migration_precision(id TEXT PRIMARY KEY, number REAL, nullable TEXT)')
    for name in ('a', 'Z', 'é', '🗿'):
        store.execute('INSERT INTO migration_precision VALUES(?,?,?)', (name, 1.2345678901234567, None))
    # Deleted high IDs must never be reused when the imported sequence resumes.
    high = 2**40
    store.execute('INSERT INTO events(id,run_id,kind,message,data,created_at) VALUES(?,?,?,?,?,?)',
                  (high, run['id'], 'status', 'Deleted event', '{}', '2026-10-09'))
    store.execute('DELETE FROM events WHERE id=?', (high,))
    app.state.test_run_id = run['id']
    yield app
    store.objects.close()


@pytest.fixture
def target():
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set MOYAI_TEST_POSTGRES_URL to a disposable Postgres database; CI provides a real service.')
    name = 'moyai_test_' + uuid4().hex
    yield url, name
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {migration.identifier(name)} CASCADE')


def exists(url, name):
    with psycopg.connect(url) as conn:
        return bool(conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (name,)).fetchone())


def test_plan_is_read_only_and_requires_no_destination(source, monkeypatch):
    store = source.state.store
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('plan must not contact Postgres'))
    report = migration.plan(store.path.parent)
    assert report['objects']['table'] >= 81
    assert report['objects']['view'] == 2 and report['objects']['trigger'] == 1
    assert report['tables']['connections']['rows'] == 1
    assert report['source_preserved'] and not report['runtime_cutover']
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    assert 'synthetic-provider-token' not in json.dumps(report)


def test_real_postgres_preserves_every_table_and_enforces_constraints(source, target):
    store, (url, name) = source.state.store, target
    before = migration.plan(store.path.parent)
    result = migration.transfer(store.path.parent, url, name)
    assert result['verified'] and result['tables'] == before['tables']
    assert result['schema_sha256'] == before['schema_sha256']
    assert migration.transfer(store.path.parent, url, name, verify_only=True)['verified']
    assert migration.plan(store.path.parent)['tables'] == before['tables']
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'SET search_path TO {migration.identifier(name)}, pg_catalog')
        assert conn.execute('SELECT data FROM attachments').fetchone()[0] == bytes(range(256))
        ciphertext = conn.execute('SELECT encrypted FROM connections').fetchone()[0]
        assert source.state.security.fernet.decrypt(ciphertext.encode()) == b'synthetic-provider-token'
        assert conn.execute('SELECT number FROM migration_precision LIMIT 1').fetchone()[0] == 1.2345678901234567
        assert conn.execute('SELECT root_id FROM run_roots').fetchone()[0] == source.state.test_run_id
        event_id = conn.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                               (source.state.test_run_id, 'status', 'New event', '{}', '2026-10-09')).fetchone()[0]
        assert event_id == 2**40 + 1
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO connection_policies(provider,enabled) VALUES('bad',3)")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES('absent','status','','{}','')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute('INSERT INTO messages(run_id,role,content,status,client_id,created_at) VALUES(%s,%s,%s,%s,%s,%s)',
                         (source.state.test_run_id, 'user', 'Duplicate', 'queued', 'initial', '2026-10-09'))


@pytest.mark.parametrize('change', ["status='stopping'", "status='cancelled'", "deleted_at='2026-10-09'"])
def test_permission_revocation_trigger_survives_the_migration(source, target, change):
    store, (url, name) = source.state.store, target
    store.execute('INSERT INTO github_write_access VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('approval', source.state.test_run_id, 'owner', 'version', 7, 9, 7, 'branch', 'main', 'PR', 'approved', '2026-10-09'))
    migration.transfer(store.path.parent, url, name)
    with psycopg.connect(url) as conn:
        conn.execute(f'SET search_path TO {migration.identifier(name)}, pg_catalog')
        conn.execute(f'UPDATE runs SET {change} WHERE id=%s', (source.state.test_run_id,))
        assert conn.execute('SELECT status FROM github_write_access').fetchone()[0] == 'revoked'


def test_failed_readback_rolls_back_everything_and_retry_succeeds(source, target, monkeypatch):
    url, name = target
    original = migration.destination_manifest
    with monkeypatch.context() as patch:
        patch.setattr(migration, 'destination_manifest', lambda *args: {})
        with pytest.raises(migration.MaintenanceError, match='verification failed'):
            migration.transfer(source.state.store.path.parent, url, name)
    assert not exists(url, name)
    assert migration.transfer(source.state.store.path.parent, url, name)['verified']
    assert migration.destination_manifest is original


def test_existing_schema_is_never_overwritten(source, target):
    url, name = target
    migration.transfer(source.state.store.path.parent, url, name)
    with pytest.raises(migration.MaintenanceError, match='already exists'):
        migration.transfer(source.state.store.path.parent, url, name)
    assert migration.transfer(source.state.store.path.parent, url, name, verify_only=True)['verified']


@pytest.mark.parametrize('change', ['extra-table', 'extra-column', 'changed-content'])
def test_independent_verification_detects_destination_drift(source, target, change):
    url, name = target
    migration.transfer(source.state.store.path.parent, url, name)
    with psycopg.connect(url) as conn:
        conn.execute(f'SET search_path TO {migration.identifier(name)}, pg_catalog')
        if change == 'extra-table':
            conn.execute('CREATE TABLE unexpected(id BIGINT PRIMARY KEY)')
        elif change == 'extra-column':
            conn.execute('ALTER TABLE runs ADD COLUMN unexpected TEXT')
        else:
            conn.execute("UPDATE runs SET prompt='incorrect copy'")
    with pytest.raises(migration.MaintenanceError):
        migration.transfer(source.state.store.path.parent, url, name, verify_only=True)


def test_concurrent_copies_publish_only_one_schema(source, target):
    url, name = target
    def copy():
        try:
            migration.transfer(source.state.store.path.parent, url, name)
            return True
        except (migration.MaintenanceError, psycopg.Error):
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: copy(), range(2))) == [False, True]
    assert migration.transfer(source.state.store.path.parent, url, name, verify_only=True)['verified']


def test_live_writes_do_not_change_the_snapshot_being_copied(source, target, monkeypatch):
    store, (url, name) = source.state.store, target
    manifest = migration.source_manifest
    expected = migration.plan(store.path.parent)['tables']
    def write_after_snapshot(conn, schema):
        result = manifest(conn, schema)
        store.execute("UPDATE runs SET summary='arrived after snapshot'")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(migration, 'source_manifest', write_after_snapshot)
        assert migration.transfer(store.path.parent, url, name)['tables'] == expected
    with pytest.raises(migration.MaintenanceError, match='verification failed'):
        migration.transfer(store.path.parent, url, name, verify_only=True)


@pytest.mark.parametrize('value', ['not-a-number', 'nul\x00text'])
def test_incompatible_source_values_fail_before_contacting_target(source, monkeypatch, value):
    store = source.state.store
    if '\x00' in value:
        store.execute('UPDATE migration_precision SET nullable=?', (value,))
    else:
        store.execute('UPDATE migration_precision SET number=?', (value,))
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: pytest.fail('source must be validated first'))
    with pytest.raises(migration.MaintenanceError):
        migration.transfer(store.path.parent, 'unused', 'moyai_invalid')


def test_unknown_and_changed_triggers_are_rejected(source):
    store = source.state.store
    store.execute('DROP TRIGGER revoke_github_write_access')
    store.execute("CREATE TRIGGER revoke_github_write_access AFTER UPDATE ON runs BEGIN SELECT 1; END")
    with pytest.raises(migration.MaintenanceError, match='trigger'):
        migration.plan(store.path.parent)


def test_broken_foreign_keys_are_rejected_without_modifying_source(source):
    store = source.state.store
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE messages SET run_id='missing'")
    before = store.path.read_bytes()
    with pytest.raises(migration.MaintenanceError, match='foreign keys'):
        migration.plan(store.path.parent)
    assert store.path.read_bytes() == before


def test_credentials_and_driver_payloads_are_not_printed(source, monkeypatch, capsys):
    monkeypatch.setenv(migration.DESTINATION_ENV, 'postgresql://password@secret-host/database')
    def failure(*args, **kwargs):
        raise RuntimeError('password@secret-host/database private message body')
    monkeypatch.setattr(migration, 'transfer', failure)
    assert migration.main(['copy', '--data-dir', str(source.state.store.path.parent), '--schema', 'moyai_redaction']) == 1
    output = capsys.readouterr().out
    assert 'password' not in output and 'private message' not in output and 'secret-host' not in output


@pytest.mark.parametrize('schema', ['public', 'moyai_"; DROP SCHEMA public; --', 'moyai_é', 'moyai_' + 'a'*60])
def test_destination_schema_is_explicit_and_restricted(schema):
    with pytest.raises(migration.MaintenanceError):
        migration.destination_schema(schema)
