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
from .database import PostgresConnection, SQLiteConnection, identifier, row_factory


ARCHIVE_LIMIT = 20 * 1024 * 1024


class MaintenanceError(ValueError):
    """Operator-facing messages containing no provider errors or secret values."""


@contextmanager
def source_database(directory: Path, *, database_url: str = '', database_schema: str = 'moyai',
                    writable: bool = False):
    """Open existing state only: no app recovery, schema upgrades or owner takeover."""
    from .db import require_database_owner
    if directory.is_symlink() or not directory.is_dir():
        raise MaintenanceError('Choose the existing application data directory, without a symlink.')
    if database_url:
        import psycopg
        schema = identifier(database_schema)
        require_database_owner(directory)
        with psycopg.connect(database_url, connect_timeout=10, cursor_factory=psycopg.ClientCursor,
                             row_factory=row_factory) as raw:
            if not writable:
                raw.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            raw.execute("SET LOCAL lock_timeout = '10s'")
            raw.execute("SET LOCAL statement_timeout = '30s'")
            raw.execute(f'SET LOCAL search_path TO {schema}, pg_catalog')
            conn = PostgresConnection(raw, database_schema, None)
            if 'runs' not in conn.table_names():
                raise MaintenanceError('Choose an existing Moyai Postgres schema. Maintenance never initializes a database.')
            yield conn
        return
    path = directory / 'workspace.db'
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError('Choose an existing database, without a symlink.')
    require_database_owner(path)
    mode = 'rw' if writable else 'ro'
    with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=' + mode, uri=True,
                                 timeout=10, factory=SQLiteConnection)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if not writable:
            conn.begin_read()
        yield conn


def table_exists(conn, name):
    return name in conn.table_names()


def attachment_columns(conn):
    return conn.column_names('attachments')


class MaintenanceStore:
    """Only payload access; deliberately does not construct the application Store."""

    def __init__(self, directory: Path, objects, *, database_url: str = '', database_schema: str = 'moyai',
                 writable: bool = True):
        from .blob_storage import ArtifactStore
        self.path = directory / 'workspace.db'
        self.objects = objects
        self.connection_options = {'database_url': database_url, 'database_schema': database_schema,
                                   'writable': writable}
        self.artifacts = ArtifactStore(self, directory, initialize=False)

    def connect(self):
        return source_database(self.path.parent, **self.connection_options)

    def rows(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]


def acknowledged_files(directory: Path, conn):
    """Only published legacy names; no staging files, symlinks or arbitrary backups."""
    root = directory / 'artifacts'
    if root.is_symlink() or not root.is_dir():
        return
    run_ids = {row['id'] for row in conn.execute('SELECT id FROM runs')}
    frozen = set()
    if 'result_snapshot' in conn.column_names('agent_groups'):
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


def plan(directory: Path, *, database_url: str = '', database_schema: str = 'moyai') -> dict:
    """Inspect without starting the app, changing schema or contacting object storage."""
    with source_database(directory, database_url=database_url, database_schema=database_schema) as conn:
        return plan_connection(directory, conn)


def plan_connection(directory: Path, conn) -> dict:
    upgraded = {'data_ref', 'preview_ref', 'preview_size'} <= attachment_columns(conn)
    pending = "data_ref='' OR ((preview_size>0 OR length(preview)>0) AND preview_ref='')" if upgraded else '1=1'
    attachments = dict(conn.execute(f'''SELECT count(*) AS count,
        coalesce(sum(length(data)+length(preview)),0) AS retained_bytes,
        coalesce(sum(CASE WHEN {pending} THEN 1 ELSE 0 END),0) AS pending
        FROM attachments''').fetchone())
    manifested = ({row['name'] for row in conn.execute("SELECT name FROM artifact_objects WHERE reference!=''")}
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
    # The worker startup guard also rejects unacknowledged files and symlinks.
    # Surface those leftovers; zero migratable records is not a cutover receipt.
    root = directory / 'artifacts'
    inventory = {'files': 0, 'unmanifested': 0, 'symlinks': int(root.is_symlink())}
    if not root.is_symlink():
        for path in root.rglob('*'):
            if path.is_symlink():
                inventory['symlinks'] += 1
            elif path.is_file() and not path.name.startswith('.upload-'):
                inventory['files'] += 1
                inventory['unmanifested'] += path.relative_to(root).as_posix() not in manifested
    return {'operation': 'plan', 'schema_ready': upgraded and table_exists(conn, 'artifact_objects'),
            'attachments': attachments, 'legacy_artifacts': artifacts, 'local_inventory': inventory,
            'remote_artifacts': len(manifested), 'source_files_preserved': True}


def verified_payload(objects, raw: bytes, reference: str = '') -> str:
    reference = reference or objects.put(raw)
    if objects.read(reference, len(raw)) != raw:
        raise MaintenanceError('Remote payload verification failed. Original bytes were preserved.')
    return reference


def migrate_attachment(store, row, clear: bool) -> tuple[bool, int]:
    data, preview = row['data'], row['preview']
    data_ref, preview_ref = row['data_ref'], row['preview_ref']
    if data or (not data_ref and row['size'] == 0):
        if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise MaintenanceError('Legacy attachment does not match its metadata. Original bytes were preserved.')
        data_ref = verified_payload(store.objects, data, data_ref)
    elif not data_ref:
        raise MaintenanceError('Legacy attachment bytes are missing. Restore the original file before migration.')
    if preview:
        preview_ref = verified_payload(store.objects, preview, preview_ref)
    elif row['preview_size'] and not preview_ref:
        raise MaintenanceError('Legacy preview bytes are missing. Restore the original file before migration.')
    cleared = len(data) + len(preview) if clear else 0
    # The remote work is finished before acquiring the database's writer lock.
    with store.connect() as conn:
        conn.begin_write()
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
        conn.begin_write()
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
    pending = "data_ref='' OR ((preview_size>0 OR length(preview)>0) AND preview_ref='')"
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
    with store.connect() as conn:
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
    with store.connect() as conn:
        result['remaining'] = plan_connection(directory, conn)
    return result


def verify(store) -> dict:
    """Read every published object back without changing references or sources."""
    if not store.objects.enabled:
        raise MaintenanceError('Configure private object storage before verification.')
    with store.connect() as conn:
        before = plan_connection(store.path.parent, conn)
    if (before['attachments']['pending'] or before['legacy_artifacts']['pending']
            or before['local_inventory']['unmanifested'] or before['local_inventory']['symlinks']):
        raise MaintenanceError('Shared file verification requires no pending payloads, unmanifested local files or symlinks. Inspect the plan on the original disk.')
    checked = 0
    # Metadata only; no database transaction is held during object downloads.
    attachments = store.rows('SELECT id,data_ref,preview_ref,size,sha256,preview_size FROM attachments ORDER BY id')
    artifacts = store.rows('SELECT name,reference,size,sha256 FROM artifact_objects ORDER BY name')
    for row in attachments + artifacts:
        reference = row.get('data_ref', row.get('reference'))
        if not reference or reference.rsplit(':', 1)[-1] != row['sha256']:
            raise MaintenanceError('Published file metadata does not match its object checksum.')
        store.objects.verify(reference, row['size'])
        checked += 1
        if row.get('preview_ref'):
            store.objects.verify(row['preview_ref'], row['preview_size'])
            checked += 1
    # A migration receipt is only meaningful for unchanged references/inventory.
    if (attachments != store.rows('SELECT id,data_ref,preview_ref,size,sha256,preview_size FROM attachments ORDER BY id')
            or artifacts != store.rows('SELECT name,reference,size,sha256 FROM artifact_objects ORDER BY name')):
        raise MaintenanceError('File inventory changed during verification. Retry after draining file writers.')
    with store.connect() as conn:
        if before != plan_connection(store.path.parent, conn):
            raise MaintenanceError('Local file inventory changed during verification. Retry on the original disk.')
    return {'operation': 'verify', 'verified_objects': checked, 'source_files_preserved': True,
            'inventory': before, 'scope': 'Current database references and this data directory; run on the original disk after draining file writers.'}


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
    parser.add_argument('operation', nargs='?', choices=('plan', 'migrate', 'verify', 'backup'), default='plan')
    parser.add_argument('--data-dir', type=Path, help='Existing Moyai DATA_DIR; defaults to deployment settings.')
    parser.add_argument('--limit', type=int, default=100, help='Maximum payload records migrated per invocation (default 100).')
    parser.add_argument('--clear-attachment-blobs', action='store_true',
                        help='Clear retained database payloads only after remote verification; does not compact the database.')
    args = parser.parse_args(argv)
    if args.clear_attachment_blobs and args.operation != 'migrate':
        parser.error('--clear-attachment-blobs applies only to migrate.')
    objects = None
    try:
        from .config import Settings
        from .blob_storage import ObjectStorage
        settings = Settings()
        directory = args.data_dir if args.data_dir is not None else settings.data_dir
        connection_options = {'database_url': settings.moyai_database_url,
                              'database_schema': settings.moyai_database_schema}
        if args.operation == 'backup' and settings.moyai_database_url:
            raise MaintenanceError('Use managed Postgres backups or pg_dump for the configured runtime database. This backup command is SQLite-only.')
        if args.operation == 'plan':
            result = plan(directory, **connection_options)
        else:
            objects = ObjectStorage(settings)
            if args.operation == 'backup':
                result = backup(directory, objects)
            else:
                if not plan(directory, **connection_options)['schema_ready']:
                    raise MaintenanceError('Upgrade all app writers before migrating payloads.')
                store = MaintenanceStore(directory, objects, writable=args.operation == 'migrate', **connection_options)
                result = (verify(store) if args.operation == 'verify' else
                          migrate(store, limit=args.limit, clear_attachment_blobs=args.clear_attachment_blobs))
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
