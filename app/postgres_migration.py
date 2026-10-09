"""Rehearse a verified SQLite-to-Postgres copy without changing the running app.

Install the postgres-migration extra. The destination must be a NEW, explicitly
named schema. This is an offline migration boundary, not a runtime SQL adapter.
"""
import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile

from .storage_maintenance import MaintenanceError, source_database


DESTINATION_ENV = 'MOYAI_MIGRATION_DATABASE_URL'
SCHEMA_PATTERN = r'moyai_[a-z][a-z0-9_]{0,49}'
# Schema SQL is executable input. Accept only the constructs used by Moyai;
# new functions, generated columns, triggers, etc. need an explicit port.
DDL_NODES = frozenset('''Create Index Identifier Table IndexParameters Ordered
    Column Where EQ Literal NEQ And Is Null Schema ColumnDef PrimaryKey
    UniqueColumnConstraint DataType ColumnConstraint NotNullColumnConstraint
    Reference PrimaryKeyColumnConstraint DefaultColumnConstraint
    AutoIncrementColumnConstraint CheckColumnConstraint In ForeignKey
    Select From With CTE Union TableAlias Join Alias'''.split())
TYPES = {'INTEGER': ('BIGINT', int), 'TEXT': ('TEXT', str),
         'REAL': ('DOUBLE', float), 'BLOB': ('BYTEA', bytes)}

# An explicit port of the permission-revocation trigger, guarded against drift.
# SQLite removes IF NOT EXISTS when it saves this definition in sqlite_schema.
REVOKE_TRIGGER = '''CREATE TRIGGER revoke_github_write_access
    AFTER UPDATE OF status,deleted_at ON runs
    WHEN NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!=''
    BEGIN UPDATE github_write_access SET status='revoked'
    WHERE run_id=NEW.id AND status IN ('pending','approved'); END'''
REVOKE_FUNCTION = '''CREATE FUNCTION revoke_github_write_access_fn() RETURNS trigger
    LANGUAGE plpgsql SET search_path FROM CURRENT AS $moyai$
    BEGIN
        UPDATE github_write_access SET status='revoked'
        WHERE run_id=NEW.id AND status IN ('pending','approved');
        RETURN NEW;
    END
    $moyai$'''
REVOKE_POSTGRES = '''CREATE TRIGGER revoke_github_write_access
    AFTER UPDATE OF status,deleted_at ON runs FOR EACH ROW
    WHEN (NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!='')
    EXECUTE FUNCTION revoke_github_write_access_fn()'''


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    primary_key: int


@dataclass
class Table:
    name: str
    columns: list[Column]
    create: str
    foreign_keys: list[str]
    identity: str | None
    sequence: int


@dataclass
class Schema:
    tables: list[Table]
    after_copy: list[str]
    fingerprint: str
    objects: dict[str, int]


def identifier(name: str) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', name):
        raise MaintenanceError('Unsupported database identifier; no destination changes were committed.')
    return '"' + name + '"'


def destination_schema(name: str) -> str:
    if not re.fullmatch(SCHEMA_PATTERN, name):
        raise MaintenanceError('Use a new schema named moyai_<name>, with lowercase letters, numbers and underscores.')
    return identifier(name)


@contextmanager
def snapshot(directory: Path):
    """A consistent, private backup includes committed WAL data; source is read-only."""
    with tempfile.TemporaryDirectory(prefix='moyai-postgres-') as temporary:
        path = Path(temporary) / 'workspace.db'
        path.touch(mode=0o600)
        with source_database(directory) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise MaintenanceError('Source snapshot failed integrity validation.')
            if target.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise MaintenanceError('Source snapshot has broken foreign keys; repair the source before copying.')
            yield target


def postgres_ddl(statement: str):
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse(statement, read='sqlite', error_level=sqlglot.ErrorLevel.RAISE)
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Create):
        raise MaintenanceError('Unsupported source schema statement; an explicit schema port is required.')
    tree = parsed[0]
    if any(type(node).__name__ not in DDL_NODES for node in tree.walk()):
        raise MaintenanceError('Unsupported source schema construct; an explicit schema port is required.')
    for node in tree.find_all(exp.Identifier):
        identifier(node.name)
    if any(node.db or node.catalog for node in tree.find_all(exp.Table)):
        raise MaintenanceError('Qualified source tables require an explicit schema port.')
    tree.set('exists', False)
    return tree


def sql(tree) -> str:
    import sqlglot
    return tree.sql(dialect='postgres', identify=True, unsupported_level=sqlglot.ErrorLevel.RAISE)


def inspect_schema(source: sqlite3.Connection) -> Schema:
    from sqlglot import exp

    entries = source.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name").fetchall()
    if not entries or not any(row['name'] == 'runs' and row['type'] == 'table' for row in entries):
        raise MaintenanceError('Source must be an initialized Moyai database.')
    tables, indexes, views, triggers = [], [], {}, []
    for row in entries:
        name, kind, ddl = row['name'], row['type'], row['sql']
        identifier(name)
        if kind == 'trigger':
            if ' '.join(ddl.split()) != ' '.join(REVOKE_TRIGGER.split()):
                raise MaintenanceError('Unknown or changed SQLite trigger; port its behavior before copying.')
            triggers.extend([REVOKE_FUNCTION, REVOKE_POSTGRES])
            continue
        tree = postgres_ddl(ddl)
        if kind == 'index':
            indexes.append(sql(tree))
            continue
        if kind == 'view':
            views[name] = tree
            continue
        if kind != 'table':
            raise MaintenanceError('Unsupported schema object.')
        columns = [Column(r['name'], r['type'].upper(), r['pk'])
                   for r in source.execute(f'PRAGMA table_xinfo({identifier(name)})')]
        if not columns or any(c.kind not in TYPES for c in columns) or not any(c.primary_key for c in columns):
            raise MaintenanceError('Every table must have a primary key and supported INTEGER, REAL, TEXT or BLOB columns.')
        primary = [c for c in columns if c.primary_key]
        identity = primary[0].name if len(primary) == 1 and primary[0].kind == 'INTEGER' else None
        foreign_keys = []
        definitions = []
        for item in tree.this.expressions:
            if isinstance(item, exp.ForeignKey):
                foreign_keys.append(f'ALTER TABLE {identifier(name)} ADD {sql(item)}')
                continue
            if isinstance(item, exp.ColumnDef):
                column = next(c for c in columns if c.name == item.name)
                item.set('kind', exp.DataType.build(TYPES[column.kind][0], dialect='postgres'))
                constraints = []
                for constraint in item.constraints:
                    if isinstance(constraint.kind, exp.Reference):
                        foreign_keys.append(f'ALTER TABLE {identifier(name)} ADD FOREIGN KEY ({identifier(column.name)}) {sql(constraint.kind)}')
                    elif not isinstance(constraint.kind, exp.AutoIncrementColumnConstraint):
                        constraints.append(constraint)
                if column.name == identity:
                    constraints.append(exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint()))
                item.set('constraints', constraints)
            definitions.append(item)
        tree.this.set('expressions', definitions)
        sequence = 0
        if identity and source.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            saved = source.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,)).fetchone()
            sequence = saved[0] if saved else 0
        tables.append(Table(name, columns, sql(tree), foreign_keys, identity, sequence))
    # Views are ordered by their dependencies, never by accidental creation order.
    ordered_views = []
    while views:
        ready = [name for name, tree in views.items()
                 if not {node.name for node in tree.expression.find_all(exp.Table)} & views.keys()]
        if not ready:
            raise MaintenanceError('Cyclic view dependencies require an explicit schema port.')
        for name in ready:
            ordered_views.append(sql(views.pop(name)))
    schema_hash = hashlib.sha256(json.dumps([tuple(r) for r in entries], ensure_ascii=False).encode()).hexdigest()
    return Schema(tables, indexes + ordered_views + triggers, schema_hash,
                  {kind: sum(r['type'] == kind for r in entries) for kind in ('table', 'index', 'view', 'trigger')})


def selection(table: Table, *, postgres: bool) -> str:
    keys = sorted((c for c in table.columns if c.primary_key), key=lambda c: c.primary_key)
    ordering = ','.join(identifier(c.name) + (' COLLATE "C"' if postgres and c.kind == 'TEXT' else '') for c in keys)
    return f'SELECT {",".join(identifier(c.name) for c in table.columns)} FROM {identifier(table.name)} ORDER BY {ordering}'


def checked_row(row, table: Table) -> tuple:
    values = []
    for value, column in zip(row, table.columns, strict=True):
        if value is None:
            if column.primary_key:
                raise MaintenanceError('Source contains a NULL primary key; repair it before copying.')
        elif column.kind == 'REAL' and type(value) is int:
            value = float(value)
        elif type(value) is not TYPES[column.kind][1]:
            raise MaintenanceError('Source contains values outside their declared column type; repair them before copying.')
        if isinstance(value, str) and '\x00' in value:
            raise MaintenanceError('Source contains NUL text, which Postgres cannot store; repair it before copying.')
        values.append(value)
    return tuple(values)


def digest_rows(rows, table: Table) -> dict:
    digest, count = hashlib.sha256(), 0
    for row in rows:
        for value in checked_row(row, table):
            if value is None:
                tag, raw = b'n', b''
            elif type(value) is int:
                tag, raw = b'i', str(value).encode('ascii')
            elif type(value) is float:
                tag, raw = b'f', struct.pack('>d', value)
            elif type(value) is bytes:
                tag, raw = b'b', value
            else:
                tag, raw = b's', value.encode('utf-8')
            digest.update(tag + len(raw).to_bytes(8, 'big') + raw)
        count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def source_manifest(source, schema: Schema) -> dict:
    return {table.name: digest_rows(source.execute(selection(table, postgres=False)), table) for table in schema.tables}


def destination_manifest(destination, schema: Schema) -> dict:
    result = {}
    for table in schema.tables:
        with destination.cursor(name='moyai_verify') as cursor:
            cursor.execute(selection(table, postgres=True))
            result[table.name] = digest_rows(cursor, table)
    return result


def verify_destination_shape(destination, schema: Schema, schema_name: str) -> None:
    actual_tables = {row[0] for row in destination.execute(
        'SELECT tablename FROM pg_tables WHERE schemaname=%s', (schema_name,))}
    if actual_tables != {table.name for table in schema.tables}:
        raise MaintenanceError('Destination table inventory differs from the source snapshot.')
    kinds = {'INTEGER': 'bigint', 'REAL': 'double precision', 'TEXT': 'text', 'BLOB': 'bytea'}
    for table in schema.tables:
        actual = list(destination.execute('''SELECT column_name,data_type,is_identity FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position''', (schema_name, table.name)))
        expected = [(c.name, kinds[c.kind], 'YES' if c.name == table.identity else 'NO') for c in table.columns]
        if actual != expected:
            raise MaintenanceError('Destination columns or types differ from the source snapshot.')


def report(operation: str, schema: Schema, manifest: dict) -> dict:
    return {'operation': operation, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'schema_sha256': schema.fingerprint, 'objects': schema.objects, 'tables': manifest,
            'total_rows': sum(item['rows'] for item in manifest.values()),
            'source_preserved': True, 'runtime_cutover': False,
            'scope': 'Database snapshot only. Preserve object storage, local artifacts and encryption/session keys separately.'}


def plan(directory: Path) -> dict:
    with snapshot(directory) as source:
        schema = inspect_schema(source)
        return report('plan', schema, source_manifest(source, schema))


def transfer(directory: Path, url: str, schema_name: str, *, verify_only: bool = False) -> dict:
    import psycopg

    target = destination_schema(schema_name)
    with snapshot(directory) as source:
        schema = inspect_schema(source)
        expected = source_manifest(source, schema)
        with psycopg.connect(url, connect_timeout=10) as destination:
            # All target DDL, data, sequences and verification share one transaction.
            # A failed copy, bad row or lost client connection rolls back the schema.
            destination.execute("SET LOCAL lock_timeout = '5s'")
            if verify_only:
                destination.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            else:
                if destination.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema_name,)).fetchone():
                    raise MaintenanceError('Destination schema already exists. Choose a new rehearsal schema; existing data is never overwritten.')
                destination.execute(f'CREATE SCHEMA {target}')
            destination.execute(f'SET LOCAL search_path TO {target}, pg_catalog')
            if not verify_only:
                for table in schema.tables:
                    destination.execute(table.create)
                    names = ','.join(identifier(c.name) for c in table.columns)
                    with destination.cursor().copy(f'COPY {identifier(table.name)} ({names}) FROM STDIN') as copy:
                        for row in source.execute(selection(table, postgres=False)):
                            copy.write_row(checked_row(row, table))
                for table in schema.tables:
                    for statement in table.foreign_keys:
                        destination.execute(statement)
                    if table.identity:
                        maximum = source.execute(f'SELECT max({identifier(table.identity)}) FROM {identifier(table.name)}').fetchone()[0] or 0
                        next_id = max(maximum, table.sequence, 0) + 1
                        if next_id > 2**63 - 1:
                            raise MaintenanceError('Source exhausted a 64-bit identity sequence.')
                        destination.execute(f'ALTER TABLE {identifier(table.name)} ALTER COLUMN {identifier(table.identity)} RESTART WITH {next_id}')
                for statement in schema.after_copy:
                    destination.execute(statement)
            verify_destination_shape(destination, schema, schema_name)
            actual = destination_manifest(destination, schema)
            if actual != expected:
                raise MaintenanceError('Row verification failed. No destination changes were committed.')
        result = report('verify' if verify_only else 'copy', schema, actual)
        result.update(destination_schema=schema_name, verified=True,
                      verification='table-inventory, column-types, row-counts and content-sha256')
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', nargs='?', choices=('plan', 'copy', 'verify'), default='plan')
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing Moyai DATA_DIR or a stopped backup copy.')
    parser.add_argument('--schema', help='New, isolated destination schema, e.g. moyai_rehearsal_20261009.')
    args = parser.parse_args(argv)
    if (args.operation == 'plan') == bool(args.schema):
        parser.error('--schema is required for copy/verify and is not used by plan.')
    try:
        if args.operation == 'plan':
            result = plan(args.data_dir)
        else:
            url = os.environ.get(DESTINATION_ENV, '')
            if not url:
                raise MaintenanceError(f'Set {DESTINATION_ENV} privately; connection strings are not accepted as command arguments.')
            result = transfer(args.data_dir, url, args.schema, verify_only=args.operation == 'verify')
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Driver/parser errors can include passwords, connection strings or row data.
        print(json.dumps({'error': str(exc) if isinstance(exc, MaintenanceError) else
                          'Migration did not complete. Source data was preserved. Check whether the destination exists and verify it before retrying.',
                          'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
