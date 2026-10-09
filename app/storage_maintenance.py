"""Explicit, resumable payload migration and verified off-volume database backups."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from . import captures


ARCHIVE_LIMIT = 20 * 1024 * 1024


class MaintenanceError(ValueError):
    """Operator-facing messages containing no provider errors or secret values."""


@contextmanager
def source_database(directory: Path):
    from .db import require_database_owner
    path = directory / 'workspace.db'
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError('Choose an existing database, without a symlink.')
    require_database_owner(path)
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def attachment_columns(conn):
    return {row['name'] for row in conn.execute('PRAGMA table_info(attachments)')}


def acknowledged_files(directory: Path, conn):
    """Only published legacy names; no staging files, symlinks or arbitrary backups."""
    root = directory / 'artifacts'
    if root.is_symlink() or not root.is_dir():
        return
    run_ids = {row['id'] for row in conn.execute('SELECT id FROM runs')}
    frozen = set()
    if 'result_snapshot' in {row['name'] for row in conn.execute('PRAGMA table_info(agent_groups)')}:
        for row in conn.execute("SELECT result_snapshot FROM agent_groups WHERE result_snapshot!=''"):
            try:
                snapshot = json.loads(row['result_snapshot'])
            except (TypeError, ValueError):
                continue
            if isinstance(snapshot, list):
                frozen.update(item['artifact_name'] for item in snapshot
                              if isinstance(item, dict) and isinstance(item.get('artifact_name'), str))
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            continue
        name = path.name
        if path.is_file() and ((re.fullmatch(r'[0-9a-f]{32}\.zip', name) and name[:-4] in run_ids)
                              or (name in frozen and re.fullmatch(r'group-[0-9a-f]{32}-[0-9a-f]{32}-[0-9]+\.zip', name))):
            yield name, ARCHIVE_LIMIT
        elif (path.is_dir() and re.fullmatch(r'[0-9a-f]{32}-captures', name) and name[:-9] in run_ids):
            for capture in sorted(path.iterdir()):
                if not capture.is_symlink() and capture.is_file() and captures.valid_name(capture.name):
                    yield name + '/' + capture.name, captures.MAX_FILE


def plan(directory: Path) -> dict:
    """Inspect without starting the app, changing schema or contacting object storage."""
    with source_database(directory) as conn:
        columns = attachment_columns(conn)
        upgraded = {'data_ref', 'preview_ref', 'preview_size'} <= columns
        pending = "(data_ref='' AND length(data)>0) OR (preview_ref='' AND length(preview)>0)" if upgraded else '1'
        attachments = dict(conn.execute(f'''SELECT count(*) AS count,
            coalesce(sum(length(data)+length(preview)),0) AS retained_bytes,
            coalesce(sum(CASE WHEN {pending} THEN 1 ELSE 0 END),0) AS pending
            FROM attachments''').fetchone())
        manifested = ({row['name'] for row in conn.execute('SELECT name FROM artifact_objects')}
                      if table_exists(conn, 'artifact_objects') else set())
        artifacts = {'count': 0, 'retained_bytes': 0, 'pending': 0}
        for name, _ in acknowledged_files(directory, conn):
            try:
                size = (directory / 'artifacts' / name).stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
            artifacts['count'] += 1
            artifacts['retained_bytes'] += size
            artifacts['pending'] += name not in manifested
        return {'operation': 'plan', 'schema_ready': upgraded and table_exists(conn, 'artifact_objects'),
                'attachments': attachments, 'legacy_artifacts': artifacts,
                'remote_artifacts': len(manifested), 'source_files_preserved': True}


def verified_payload(objects, raw: bytes, reference: str = '') -> str:
    reference = reference or objects.put(raw)
    if objects.read(reference, len(raw)) != raw:
        raise MaintenanceError('Remote payload verification failed. Original bytes were preserved.')
    return reference


def migrate_attachment(store, row, clear: bool) -> tuple[bool, int]:
    data, preview = row['data'], row['preview']
    data_ref, preview_ref = row['data_ref'], row['preview_ref']
    if data:
        if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise MaintenanceError('Legacy attachment does not match its metadata. Original bytes were preserved.')
        data_ref = verified_payload(store.objects, data, data_ref)
    elif not data_ref:
        raise MaintenanceError('Legacy attachment bytes are missing. Restore the original file before migration.')
    if preview:
        preview_ref = verified_payload(store.objects, preview, preview_ref)
    cleared = len(data) + len(preview) if clear else 0
    # The remote work is finished before acquiring the database's writer lock.
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM attachments WHERE id=?', (row['id'],)).fetchone()
        if current is None or dict(current) != row:
            return False, 0
        conn.execute('''UPDATE attachments SET data_ref=?,preview_ref=?,preview_size=?,data=?,preview=? WHERE id=?''',
                     (data_ref, preview_ref, len(preview) if preview else row['preview_size'],
                      b'' if clear else data, b'' if clear else preview, row['id']))
    return True, cleared


def migrate_artifact(store, name: str, limit: int) -> bool:
    before = store.artifacts.info(name)
    if before is None or before['reference']:
        return False
    raw = store.artifacts.read(name, limit, revision=before['revision'])
    if '/' in name:
        captures.media_type(name.rsplit('/', 1)[-1], raw)
    reference = verified_payload(store.objects, raw)
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = store.artifacts.info(name, conn)
        if current != before:
            return False
        conn.execute('''INSERT INTO artifact_objects(name,reference,size,sha256,created_at)
            VALUES(?,?,?,?,?)''', (name, reference, len(raw), hashlib.sha256(raw).hexdigest(),
                                  datetime.now(timezone.utc).isoformat()))
    return True


def migrate(store, *, limit: int = 100, clear_attachment_blobs: bool = False) -> dict:
    if not store.objects.enabled:
        raise MaintenanceError('Configure private object storage before migration.')
    if not 1 <= limit <= 10000:
        raise MaintenanceError('Use a migration batch size between 1 and 10000.')
    directory = store.path.parent
    pending = "(data_ref='' AND length(data)>0) OR (preview_ref='' AND length(preview)>0)"
    if clear_attachment_blobs:
        pending += ' OR length(data)>0 OR length(preview)>0'
    # Enumerate IDs only; at most one attachment's bytes are held at a time.
    ids = store.rows(f'SELECT id FROM attachments WHERE {pending} ORDER BY id LIMIT ?', (limit,))
    result = {'operation': 'migrate', 'attachments_published': 0, 'artifacts_published': 0,
              'superseded': 0, 'attachment_bytes_cleared': 0, 'source_files_preserved': True}
    for item in ids:
        rows = store.rows('SELECT * FROM attachments WHERE id=?', (item['id'],))
        if not rows:
            result['superseded'] += 1
            continue
        published, cleared = migrate_attachment(store, rows[0], clear_attachment_blobs)
        result['attachments_published' if published else 'superseded'] += 1
        result['attachment_bytes_cleared'] += cleared
    remaining = limit - len(ids)
    with source_database(directory) as conn:
        names = list(acknowledged_files(directory, conn))
    for name, bound in names:
        if remaining == 0:
            break
        current = store.artifacts.info(name)
        if current is None or current['reference']:
            continue
        published = migrate_artifact(store, name, bound)
        result['artifacts_published' if published else 'superseded'] += 1
        remaining -= 1
    result['remaining'] = plan(directory)
    return result


def backup(directory: Path, objects) -> dict:
    if not objects.enabled:
        raise MaintenanceError('Configure private object storage before backup.')
    # System temporary storage avoids another database-sized file on DATA_DIR.
    with tempfile.TemporaryDirectory(prefix='moyai-backup-') as temporary:
        root = Path(temporary)
        if root.resolve().is_relative_to(directory.resolve()):
            raise MaintenanceError('System temporary storage must be outside DATA_DIR.')
        snapshot = root / 'workspace.db'
        with source_database(directory) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise MaintenanceError('Database backup failed integrity validation.')
        compressed = root / 'workspace.db.gz'
        with snapshot.open('rb') as source, compressed.open('wb') as destination:
            with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as stream:
                shutil.copyfileobj(source, stream, length=1024 * 1024)
        with compressed.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        size = compressed.stat().st_size
        reference = objects.put_file(compressed)
        objects.verify(reference, size)
        return {'operation': 'backup', 'reference': reference, 'sha256': checksum, 'size': size,
                'format': 'sqlite3+gzip', 'created_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'Database only; preserve referenced objects, legacy files and encryption keys separately.'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', choices=('plan', 'migrate', 'backup'), default='plan')
    parser.add_argument('--data-dir', type=Path, help='Existing Moyai DATA_DIR; defaults to deployment settings.')
    parser.add_argument('--limit', type=int, default=100, help='Maximum payload records migrated per invocation (default 100).')
    parser.add_argument('--clear-attachment-blobs', action='store_true',
                        help='Clear retained SQLite BLOBs only after remote verification; does not compact SQLite.')
    args = parser.parse_args(argv)
    if args.clear_attachment_blobs and args.operation != 'migrate':
        parser.error('--clear-attachment-blobs applies only to migrate.')
    objects = None
    try:
        from .config import Settings
        from .blob_storage import ObjectStorage
        settings = Settings()
        if settings.moyai_database_url:
            raise MaintenanceError('This maintenance command is SQLite-only. Use Postgres backups for the configured runtime database.')
        directory = args.data_dir if args.data_dir is not None else settings.data_dir
        if args.operation == 'plan':
            result = plan(directory)
        else:
            objects = ObjectStorage(settings)
            if args.operation == 'backup':
                result = backup(directory, objects)
            else:
                if not plan(directory)['schema_ready']:
                    raise MaintenanceError('Upgrade all app writers before migrating payloads.')
                from .db import Store
                store = Store(directory, object_storage=objects)
                result = migrate(store, limit=args.limit, clear_attachment_blobs=args.clear_attachment_blobs)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Never print SDK exception strings: endpoints and credentials may occur there.
        print(json.dumps({'error': str(exc) if isinstance(exc, MaintenanceError) else
                          'Storage maintenance failed. Earlier verified records remain safe; retry after correcting the cause.',
                          'error_type': type(exc).__name__}))
        return 1
    finally:
        if objects is not None:
            objects.close()


if __name__ == '__main__':
    raise SystemExit(main())
