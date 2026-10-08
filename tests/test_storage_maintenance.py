import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app import storage_maintenance as maintenance
from app.config import Settings
from app.db import Store
from storage_fixture import MemoryObjects


def legacy_attachment(store):
    attachment_id = uuid4().hex
    store.attachments.save(attachment_id, 'owner', 'image.png', b'original',
                           ('image/png', b'preview', ''), 1024)
    return attachment_id


def test_default_plan_does_not_upgrade_legacy_database_or_contact_remote(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    legacy_attachment(store)
    with store.connect() as conn:
        for column in ('data_ref', 'preview_ref', 'preview_size'):
            conn.execute(f'ALTER TABLE attachments DROP COLUMN {column}')
        conn.execute('DROP TABLE artifact_objects')
    before = store.path.read_bytes()
    monkeypatch.setattr('app.config.Settings', lambda: Settings(_env_file=None, data_dir=tmp_path))
    monkeypatch.setattr('app.blob_storage.ObjectStorage', lambda *args: pytest.fail('Plan must not construct a backend'))
    assert maintenance.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['operation'] == 'plan' and not report['schema_ready']
    assert report['attachments'] == {'count': 1, 'pending': 1, 'retained_bytes': 15}
    assert store.path.read_bytes() == before
    with maintenance.source_database(tmp_path) as conn:
        assert not {'data_ref', 'preview_ref', 'preview_size'} & maintenance.attachment_columns(conn)
        assert not maintenance.table_exists(conn, 'artifact_objects')


def test_migration_keeps_sources_migrates_frozen_handoffs_and_resumes(tmp_path):
    store = Store(tmp_path)
    attachment_id = legacy_attachment(store)
    run_id = store.create_run('Existing session', '', 'demo', [])['id']
    frozen = f'group-{uuid4().hex}-{run_id}-1.zip'
    root = tmp_path / 'artifacts'
    root.mkdir()
    (root / (run_id + '.zip')).write_bytes(b'latest archive')
    (root / frozen).write_bytes(b'frozen archive')
    store.execute('CREATE TABLE agent_groups(result_snapshot TEXT)')
    store.execute('INSERT INTO agent_groups VALUES(?)', (json.dumps([{'artifact_name': frozen}]),))
    directory = root / (run_id + '-captures')
    directory.mkdir()
    (directory / 'proof.png').write_bytes(b'\x89PNG\r\n\x1a\nproof')
    (directory / 'unfinished.next').write_bytes(b'partial')
    (directory / 'symlink.png').symlink_to(directory / 'proof.png')
    (root / (uuid4().hex + '.zip')).write_bytes(b'unacknowledged run')
    (root / f'group-{uuid4().hex}-{run_id}-2.zip').write_bytes(b'unpublished handoff')
    (root / (run_id + '.next')).write_bytes(b'partial')
    store.objects = objects = MemoryObjects()
    first = maintenance.migrate(store, limit=1)
    assert first['attachments_published'] == 1 and first['artifacts_published'] == 0
    row = store.rows('SELECT * FROM attachments WHERE id=?', (attachment_id,))[0]
    assert row['data'] == b'original' and row['preview'] == b'preview'
    assert row['preview_size'] == 7 and row['data_ref'] and row['preview_ref']
    second = maintenance.migrate(store)
    assert second['artifacts_published'] == 3 and second['attachments_published'] == 0
    assert {row['name'] for row in store.rows('SELECT name FROM artifact_objects')} == {
        run_id + '.zip', frozen, run_id + '-captures/proof.png'}
    assert (root / frozen).read_bytes() == b'frozen archive'
    assert second['remaining']['legacy_artifacts']['pending'] == 0
    assert second['remaining']['attachments']['pending'] == 0
    reads = objects.reads
    assert maintenance.migrate(store)['artifacts_published'] == 0
    assert objects.reads == reads
    reopened = Store(tmp_path, object_storage=objects)
    assert reopened.attachments.payload(row) == b'original'
    assert reopened.artifacts.read(frozen, 100) == b'frozen archive'


@pytest.mark.parametrize('phase', ['original', 'preview'])
def test_corrupt_readback_cannot_publish_or_clear_any_attachment_payload(tmp_path, phase):
    store = Store(tmp_path)
    attachment_id = legacy_attachment(store)
    objects = MemoryObjects()
    store.objects = objects
    read = objects.read

    def corrupt(reference, limit):
        raw = read(reference, limit)
        return b'wrong' if raw == (b'original' if phase == 'original' else b'preview') else raw

    objects.read = corrupt
    with pytest.raises(ValueError, match='verification'):
        maintenance.migrate(store, clear_attachment_blobs=True)
    row = store.rows('SELECT * FROM attachments WHERE id=?', (attachment_id,))[0]
    assert not row['data_ref'] and not row['preview_ref']
    assert row['data'] == b'original' and row['preview'] == b'preview'


def test_remote_io_releases_writer_lock_and_new_attachment_reference_wins(tmp_path):
    store = Store(tmp_path)
    attachment_id = legacy_attachment(store)
    store.objects = objects = MemoryObjects()
    read, put = objects.read, objects.put
    changed = False

    def verify_unlocked(reference, limit):
        nonlocal changed
        # This would raise immediately if migration held SQLite's writer lock.
        with sqlite3.connect(store.path, timeout=0) as writer:
            writer.execute('BEGIN IMMEDIATE')
            if not changed:
                replacement = put(b'newer')
                writer.execute("UPDATE attachments SET data_ref=?,data=x'',size=5,sha256=? WHERE id=?",
                               (replacement, hashlib.sha256(b'newer').hexdigest(), attachment_id))
                changed = True
        return read(reference, limit)

    objects.read = verify_unlocked
    report = maintenance.migrate(store, clear_attachment_blobs=True)
    assert report['superseded'] == 1 and report['attachment_bytes_cleared'] == 0
    row = store.rows('SELECT * FROM attachments WHERE id=?', (attachment_id,))[0]
    assert row['data_ref'] == put(b'newer') and not row['preview_ref']
    assert row['preview'] == b'preview'


@pytest.mark.parametrize('change', ['remote', 'local'])
def test_artifact_publication_rechecks_remote_winner_and_local_revision(tmp_path, change):
    store = Store(tmp_path)
    run_id = store.create_run('Existing session', '', 'demo', [])['id']
    name = run_id + '.zip'
    store.artifacts.save(name, b'original archive')
    store.objects = objects = MemoryObjects()
    read = objects.read
    changed = False

    def race(reference, limit):
        nonlocal changed
        if not changed:
            changed = True
            if change == 'remote':
                store.artifacts.save(name, b'newer remote archive')
            else:
                store.artifacts.path(name).write_bytes(b'newer local archive, longer')
        return read(reference, limit)

    objects.read = race
    report = maintenance.migrate(store)
    assert report['superseded'] == 1 and report['artifacts_published'] == 0
    assert store.artifacts.read(name, 100) == (b'newer remote archive' if change == 'remote' else b'newer local archive, longer')


def test_explicit_blob_clear_reverifies_existing_objects_and_never_unlinks_artifacts(tmp_path):
    store = Store(tmp_path)
    attachment_id = legacy_attachment(store)
    store.objects = objects = MemoryObjects()
    maintenance.migrate(store)
    row = store.rows('SELECT * FROM attachments WHERE id=?', (attachment_id,))[0]
    objects.values[row['preview_ref']] = b'corrupt'
    with pytest.raises(ValueError, match='verification'):
        maintenance.migrate(store, clear_attachment_blobs=True)
    assert store.rows('SELECT length(data)+length(preview) AS size FROM attachments')[0]['size'] == 15
    objects.values[row['preview_ref']] = b'preview'
    report = maintenance.migrate(store, clear_attachment_blobs=True)
    assert report['attachment_bytes_cleared'] == 15
    row = store.rows('SELECT * FROM attachments WHERE id=?', (attachment_id,))[0]
    assert row['data'] == row['preview'] == b''
    assert store.attachments.payload(row, 'preview') == b'preview'
    assert maintenance.migrate(store, clear_attachment_blobs=True)['attachments_published'] == 0


def test_backup_includes_committed_wal_uses_external_temp_and_returns_verified_receipt(tmp_path):
    directory = tmp_path / 'data'
    store = Store(directory)
    objects = MemoryObjects()
    staged = []
    put_file = objects.put_file

    def capture(path):
        staged.append(path)
        assert not path.resolve().is_relative_to(directory.resolve())
        return put_file(path)

    objects.put_file = capture
    with sqlite3.connect(store.path) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL')
        store.create_run('Committed WAL state', '', 'demo', [])
        assert Path(str(store.path) + '-wal').stat().st_size > 0
        receipt = maintenance.backup(directory, objects)
    assert receipt['format'] == 'sqlite3+gzip' and receipt['operation'] == 'backup'
    raw = objects.values[receipt['reference']]
    assert hashlib.sha256(raw).hexdigest() == receipt['sha256'] and len(raw) == receipt['size']
    assert objects.reads == 1
    restored = tmp_path / 'restored.db'
    restored.write_bytes(gzip.decompress(raw))
    with sqlite3.connect(restored) as conn:
        assert conn.execute('SELECT prompt FROM runs').fetchone()[0] == 'Committed WAL state'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not staged[0].exists()
    assert not store.rows('SELECT * FROM artifact_objects')
    assert not list(directory.glob('*backup*'))


def test_backup_failure_has_no_receipt_cleans_temp_and_sanitizes_sdk_details(tmp_path, monkeypatch, capsys):
    Store(tmp_path)
    objects = MemoryObjects()
    staged = []
    put_file = objects.put_file

    def capture(path):
        staged.append(path)
        return put_file(path)

    def fail(reference, size):
        raise RuntimeError('private-credential-in-sdk-error')

    objects.put_file, objects.verify = capture, fail
    monkeypatch.setattr('app.config.Settings', lambda: Settings(_env_file=None, data_dir=tmp_path))
    monkeypatch.setattr('app.blob_storage.ObjectStorage', lambda settings: objects)
    assert maintenance.main(['backup']) == 1
    output = capsys.readouterr().out
    assert 'private-credential' not in output and 'reference' not in json.loads(output)
    assert json.loads(output)['error_type'] == 'RuntimeError'
    assert not staged[0].exists()


@pytest.mark.parametrize('operation', ['plan', 'backup'])
def test_read_only_maintenance_requires_owner_before_sqlite_open(tmp_path, monkeypatch, operation):
    Store(tmp_path)
    owner = (tmp_path / 'workspace.db').stat().st_uid
    monkeypatch.setattr('app.db.os.geteuid', lambda: owner + 1)
    monkeypatch.setattr(maintenance.sqlite3, 'connect', lambda *a, **kw: pytest.fail('Wrong UID opened SQLite'))
    with pytest.raises(PermissionError, match='database owner'):
        if operation == 'plan':
            maintenance.plan(tmp_path)
        else:
            maintenance.backup(tmp_path, MemoryObjects())
