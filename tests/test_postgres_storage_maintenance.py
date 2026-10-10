"""Payload maintenance alongside a live application owner on real PostgreSQL."""
import hashlib
import json
import os
from uuid import uuid4

import psycopg
import pytest

from app import storage_maintenance as maintenance
from app.config import Settings
from app.db import Store
from storage_fixture import MemoryObjects


@pytest.fixture
def source(tmp_path):
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_files_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    store = Store(tmp_path, database_url=url, database_schema=schema,
                  database_initialize=True, application_instance=True)
    options = {'database_url': url, 'database_schema': schema}
    try:
        yield store, options
    finally:
        store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def seed(store):
    attachment = uuid4().hex
    store.attachments.save(attachment, 'synthetic-owner', 'image.png', b'original',
                           ('image/png', b'preview', ''), 1024)
    run = store.create_run('Storage migration test', '', 'demo', [])['id']
    store.artifacts.save(run + '.zip', b'archive')
    store.artifacts.save(run + '-captures/proof.png', b'\x89PNG\r\n\x1a\nproof')
    frozen = f'group-{uuid4().hex}-{run}-1.zip'
    store.artifacts.save(frozen, b'frozen')
    store.execute('CREATE TABLE agent_groups(result_snapshot TEXT)')
    store.execute('INSERT INTO agent_groups VALUES(?)', (json.dumps([{'artifact_name': frozen}]),))
    return attachment, run, frozen


def test_cli_migrates_and_verifies_while_app_owns_postgres_without_touching_sqlite(source, monkeypatch, capsys):
    store, options = source
    attachment, run, frozen = seed(store)
    # A stale file cannot be parsed, upgraded, or used as a fallback.
    store.path.write_bytes(b'preserved stale SQLite sentinel')
    files = {path: path.read_bytes() for path in store.artifacts.root.rglob('*') if path.is_file()}
    settings = Settings(_env_file=None, data_dir=store.path.parent,
                        moyai_database_url=options['database_url'], moyai_database_schema=options['database_schema'])
    objects = MemoryObjects()
    monkeypatch.setattr('app.config.Settings', lambda: settings)
    monkeypatch.setattr('app.db.Store', lambda *a, **kw: pytest.fail('Maintenance started the application Store'))
    monkeypatch.setattr('app.blob_storage.ObjectStorage', lambda *a: pytest.fail('Plan constructed object storage'))
    assert maintenance.main(['plan']) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial['attachments']['pending'] == 1 and initial['legacy_artifacts']['pending'] == 3
    monkeypatch.setattr('app.blob_storage.ObjectStorage', lambda *a: objects)
    assert maintenance.main(['migrate', '--limit', '1']) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['attachments_published'] == 1 and first['artifacts_published'] == 0
    assert maintenance.main(['migrate']) == 0
    second = json.loads(capsys.readouterr().out)
    assert second['artifacts_published'] == 3
    assert maintenance.main(['verify']) == 0
    assert json.loads(capsys.readouterr().out)['verified_objects'] == 5
    assert maintenance.main(['migrate']) == 0
    assert json.loads(capsys.readouterr().out)['artifacts_published'] == 0
    assert store.path.read_bytes() == b'preserved stale SQLite sentinel'
    assert all(path.read_bytes() == raw for path, raw in files.items())
    assert store.rows('SELECT data,preview FROM attachments WHERE id=?', (attachment,))[0] == {
        'data': b'original', 'preview': b'preview'}
    store.update_run(run, summary='Application owner remains live')
    assert store.run(run)['summary'] == 'Application owner remains live'


def test_read_only_plan_never_initializes_or_writes_and_rejects_wrong_schema(source):
    store, options = source
    with maintenance.source_database(store.path.parent, **options) as conn:
        assert conn.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute('CREATE TABLE forbidden_table(id INTEGER)')
    with pytest.raises(maintenance.MaintenanceError, match='existing Moyai Postgres schema'):
        maintenance.plan(store.path.parent, **(options | {'database_schema': 'absent_' + uuid4().hex}))
    assert not store.path.exists()


def test_missing_payload_columns_are_reported_without_schema_changes(source):
    store, options = source
    with store.connect() as conn:
        conn.execute('ALTER TABLE attachments DROP COLUMN preview_ref')
    assert not maintenance.plan(store.path.parent, **options)['schema_ready']
    with store.connect() as conn:
        assert 'preview_ref' not in conn.column_names('attachments')


@pytest.mark.parametrize('kind', ['attachment', 'artifact'])
def test_remote_readback_releases_postgres_lock_and_newer_publication_wins(source, kind):
    store, options = source
    attachment, run, _ = seed(store)
    objects = MemoryObjects()
    worker = maintenance.MaintenanceStore(store.path.parent, objects, **options)
    original_read = objects.read
    changed = False

    def concurrent_write(reference, limit):
        nonlocal changed
        # A separate application connection must take the real writer lock
        # during object I/O. It fails quickly if maintenance still holds it.
        if not changed:
            changed = True
            with store.connect() as conn:
                conn.raw.execute("SET LOCAL lock_timeout = '200ms'")
                conn.begin_write()
                if kind == 'attachment':
                    conn.execute('UPDATE attachments SET name=? WHERE id=?', ('newer.png', attachment))
                else:
                    raw = b'new archive'
                    conn.execute('''INSERT INTO artifact_objects VALUES(?,?,?,?,?)''',
                                 (run + '.zip', objects.put(raw), len(raw), hashlib.sha256(raw).hexdigest(), '2026-10-10'))
        return original_read(reference, limit)

    objects.read = concurrent_write
    if kind == 'attachment':
        result = maintenance.migrate(worker, limit=1, clear_attachment_blobs=True)
        assert result['superseded'] == 1 and result['attachment_bytes_cleared'] == 0
        assert store.rows('SELECT data_ref,name FROM attachments WHERE id=?', (attachment,))[0] == {
            'data_ref': '', 'name': 'newer.png'}
    else:
        assert not maintenance.migrate_artifact(worker, run + '.zip', 100)
        assert worker.artifacts.read(run + '.zip', 100) == b'new archive'


def test_corrupt_object_does_not_publish_or_clear_postgres_payload(source):
    store, options = source
    attachment, _, _ = seed(store)
    objects = MemoryObjects()
    worker = maintenance.MaintenanceStore(store.path.parent, objects, **options)
    objects.read = lambda *a: b'corrupt'
    with pytest.raises(maintenance.MaintenanceError, match='verification'):
        maintenance.migrate(worker, limit=1, clear_attachment_blobs=True)
    row = store.rows('SELECT data,preview,data_ref FROM attachments WHERE id=?', (attachment,))[0]
    assert row == {'data': b'original', 'preview': b'preview', 'data_ref': ''}


def test_replacement_with_empty_local_disk_reads_all_migrated_payloads(source, tmp_path):
    store, options = source
    attachment, run, frozen = seed(store)
    objects = MemoryObjects()
    worker = maintenance.MaintenanceStore(store.path.parent, objects, **options)
    maintenance.migrate(worker, clear_attachment_blobs=True)
    assert maintenance.verify(worker)['verified_objects'] == 5
    original = store.path.parent
    store.close()
    replacement = Store(tmp_path / 'replacement', object_storage=objects, **options, application_instance=True)
    try:
        assert not replacement.artifacts.root.exists()
        row = replacement.rows('SELECT * FROM attachments WHERE id=?', (attachment,))[0]
        assert row['data'] == row['preview'] == b''
        assert replacement.attachments.payload(row) == b'original'
        assert replacement.attachments.payload(row, 'preview') == b'preview'
        assert replacement.artifacts.read(run + '.zip', 100) == b'archive'
        assert replacement.artifacts.read(frozen, 100) == b'frozen'
        assert replacement.artifacts.read(run + '-captures/proof.png', 100).endswith(b'proof')
        assert not (original / 'workspace.db').exists()
    finally:
        replacement.close()


def test_postgres_backup_refuses_stale_sqlite_and_sanitizes_connection_errors(source, monkeypatch, capsys):
    store, options = source
    settings = Settings(_env_file=None, data_dir=store.path.parent,
                        moyai_database_url=options['database_url'], moyai_database_schema=options['database_schema'])
    monkeypatch.setattr('app.config.Settings', lambda: settings)
    monkeypatch.setattr('app.blob_storage.ObjectStorage', lambda *a: pytest.fail('Backup opened storage'))
    assert maintenance.main(['backup']) == 1
    assert 'managed Postgres backups' in json.loads(capsys.readouterr().out)['error']
    with monkeypatch.context() as patch:
        patch.setattr(psycopg, 'connect', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('secret-database-url')))
        assert maintenance.main(['plan']) == 1
        assert 'secret-database-url' not in capsys.readouterr().out
