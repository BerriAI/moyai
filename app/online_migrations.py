"""Reviewed additive DDL without application startup or global writer exclusion.

An installed bridge can execute a newer release's declarative plan. Plans contain
no SQL and must start at a locally known schema digest or a verified receipt chain.
DDL, receipts, and schema revision commit together. Statement failure rolls back;
a lost commit acknowledgement requires inspection of the committed receipts.
"""
import hashlib
import json
from pathlib import Path
import re
import time

from .database import DatabaseError, identifier

PROTOCOL = 1
MIGRATION_LOCK = 726950
LOCK_TIMEOUT_MS = 250
STATEMENT_TIMEOUT_MS = 5000
TRANSACTION_TIMEOUT_MS = 20000
LEDGER = 'online_schema_migrations'
TYPES = {'TEXT', 'BIGINT', 'INTEGER', 'BOOLEAN', 'TIMESTAMPTZ', 'JSONB',
         'BYTEA', 'DOUBLE PRECISION', 'UUID'}
STEP_FIELDS = {'from_revision', 'to_revision', 'compatibility', 'source_digest',
               'target_digest', 'operations'}
PLAN_FIELDS = {'from_revision', 'to_revision', 'compatibility', 'steps', 'digest'}


def require(condition, message):
    if not condition:
        raise DatabaseError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def schema_support():
    from .schema_migrations import SCHEMA_REVISION, SCHEMA_COMPATIBILITY_REVISION
    return {'protocol': PROTOCOL, 'required': SCHEMA_REVISION,
            'compatibility': SCHEMA_COMPATIBILITY_REVISION}


def _versions():
    return json.loads(Path(__file__).with_name('schema_versions.json').read_text())


def _manifest():
    return json.loads(Path(__file__).with_name('online_schema_migrations.json').read_text())


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _name(value):
    require(isinstance(value, str) and re.fullmatch('[a-z][a-z0-9_]{0,62}', value) is not None,
            'Online migration contains an invalid identifier.')
    return identifier(value)


def _table(value):
    _name(value)
    require(value not in {'schema_state', LEDGER}, 'Online migration cannot modify its metadata.')


def _column(value, *, nullable_only=False):
    require(type(value) is dict and set(value) == {'name', 'type', 'nullable'},
            'Online migration columns require an explicit type and nullability; defaults are forbidden.')
    _name(value['name'])
    require(isinstance(value['type'], str) and value['type'] in TYPES and type(value['nullable']) is bool
            and (not nullable_only or value['nullable']), 'Online migration column is not additive.')


def _step(value):
    require(type(value) is dict and set(value) == STEP_FIELDS, 'Online migration step fields are invalid.')
    require(all(type(value[k]) is int and value[k] > 0 for k in
                ('from_revision', 'to_revision', 'compatibility'))
            and value['to_revision'] == value['from_revision'] + 1
            and value['from_revision'] >= value['compatibility'], 'Online migrations must be contiguous expansions.')
    require(_hash(value['source_digest']) and _hash(value['target_digest'])
            and value['source_digest'] != value['target_digest'], 'Online migration schema digests are invalid.')
    operations = value['operations']
    require(type(operations) is list and 0 < len(operations) <= 64, 'Online migration operations are invalid.')
    created, indexes, columns = {}, set(), set()
    for op in operations:
        require(type(op) is dict, 'Online migration operation must be declarative.')
        kind = op.get('kind')
        if kind == 'create_table':
            require(set(op) == {'kind', 'name', 'columns', 'primary_key'}, 'Online table fields are invalid.')
            _table(op['name'])
            require(op['name'] not in created and type(op['columns']) is list
                    and 0 < len(op['columns']) <= 64, 'Online table columns are invalid.')
            for column in op['columns']:
                _column(column)
            names = {c['name'] for c in op['columns']}
            require(len(names) == len(op['columns']), 'Online table has duplicate columns.')
            primary = op['primary_key']
            require(type(primary) is list and all(isinstance(c, str) for c in primary)
                    and len(primary) == len(set(primary))
                    and set(primary) <= names, 'Online table primary key is invalid.')
            require(all(not c['nullable'] for c in op['columns'] if c['name'] in primary),
                    'Online primary-key columns must be non-null.')
            created[op['name']] = names
        elif kind == 'add_column':
            require(set(op) == {'kind', 'table', 'name', 'type', 'nullable'}, 'Online column fields are invalid.')
            _table(op['table'])
            _column({k: op[k] for k in ('name', 'type', 'nullable')}, nullable_only=True)
            key = op['table'], op['name']
            require(key not in columns, 'Online migration repeats a column.')
            columns.add(key)
        elif kind == 'create_index':
            require(set(op) == {'kind', 'name', 'table', 'columns', 'unique'}, 'Online index fields are invalid.')
            _name(op['name'])
            _table(op['table'])
            require(op['table'] in created and type(op['columns']) is list
                    and 0 < len(op['columns']) <= 64 and type(op['unique']) is bool
                    and op['name'] not in indexes, 'Online indexes require a table created in the same step.')
            require(all(isinstance(c, str) for c in op['columns'])
                    and len(op['columns']) == len(set(op['columns']))
                    and set(op['columns']) <= created[op['table']], 'Online index columns are invalid.')
            indexes.add(op['name'])
        else:
            raise DatabaseError('Online migration operation is not an approved additive operation.')
    return value


def validate_plan(value):
    """Pure protocol validation; an older bridge need not know future manifests."""
    require(type(value) is dict and set(value) == PLAN_FIELDS, 'Online migration plan fields are invalid.')
    require(all(type(value[k]) is int and value[k] > 0 for k in
                ('from_revision', 'to_revision', 'compatibility'))
            and value['compatibility'] <= value['from_revision'] <= value['to_revision'],
            'Online migration plan revisions are invalid.')
    require(type(value['steps']) is list and len(value['steps']) <= 32, 'Online migration step count is invalid.')
    revision, previous_digest = value['from_revision'], None
    for step in value['steps']:
        _step(step)
        require(step['from_revision'] == revision and step['compatibility'] == value['compatibility']
                and (previous_digest is None or step['source_digest'] == previous_digest),
                'Online migration plan has a revision or digest gap.')
        revision, previous_digest = step['to_revision'], step['target_digest']
    require(revision == value['to_revision'], 'Online migration plan does not reach its target.')
    require(_hash(value['digest']) and value['digest'] == digest({k: v for k, v in value.items() if k != 'digest'}),
            'Online migration plan digest changed.')
    return value


def plan(current_revision, target_revision=None):
    """Read the release's reviewed manifest; missing steps require maintenance."""
    support = schema_support()
    target = support['required'] if target_revision is None else target_revision
    require(type(current_revision) is int and type(target) is int
            and support['compatibility'] <= current_revision <= target <= support['required'],
            'No compatible online migration path; use reviewed maintenance.')
    manifest = _manifest()
    require(type(manifest) is dict and set(manifest) == {'protocol', 'steps'}
            and manifest['protocol'] == PROTOCOL and type(manifest['steps']) is list,
            'Online migration manifest is invalid.')
    versions, by_revision, previous = _versions(), {}, None
    for step in manifest['steps']:
        _step(step)
        start = step['from_revision']
        require(step['compatibility'] == support['compatibility'] and start not in by_revision
                and (previous is None or start == previous), 'Online migration manifest is not contiguous.')
        require(versions.get(str(start)) == step['source_digest']
                and versions.get(str(step['to_revision'])) == step['target_digest'],
                'Online migration manifest differs from the versioned schema source.')
        by_revision[start], previous = step, step['to_revision']
    steps = []
    for revision in range(current_revision, target):
        require(revision in by_revision, 'No reviewed online migration step; use maintenance.')
        steps.append(by_revision[revision])
    result = {'from_revision': current_revision, 'to_revision': target,
              'compatibility': support['compatibility'], 'steps': steps}
    result['digest'] = digest(result)
    return validate_plan(result)


def _state(conn, schema, *, for_update=False):
    table = identifier(schema) + '.schema_state'
    if not conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]:
        return None
    return conn.execute(f'SELECT revision,dirty FROM {table} WHERE id=1' +
                        (' FOR UPDATE' if for_update else '')).fetchone()


def _has_incoming_receipt(conn, schema, revision):
    table = identifier(schema) + '.' + identifier(LEDGER)
    return bool(conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]
                and conn.execute(f'SELECT 1 FROM {table} WHERE to_revision=%s', (revision,)).fetchone())


def _chain(conn, schema, required, actual, compatibility):
    """Receipt chain anchored in this binary's known required-schema digest."""
    anchor = _versions().get(str(required))
    if not anchor or required < compatibility or actual < required:
        return None
    if actual == required:
        if _has_incoming_receipt(conn, schema, required):
            return anchor if _chain(conn, schema, required - 1, actual, compatibility) == anchor else None
        return anchor
    table = identifier(schema) + '.' + identifier(LEDGER)
    if not conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]:
        return None
    rows = conn.execute(f'''SELECT from_revision,to_revision,compatibility,
        source_digest,target_digest,step_digest,step FROM {table}
        WHERE from_revision>=%s AND to_revision<=%s ORDER BY from_revision''', (required, actual)).fetchall()
    revision = required
    for start, end, compat, source, target, step_digest, step in rows:
        try:
            _step(step)
        except (DatabaseError, TypeError, ValueError):
            return None
        if (start != revision or end != start + 1 or compat != compatibility or source != anchor
                or step != {'from_revision':start,'to_revision':end,'compatibility':compat,
                            'source_digest':source,'target_digest':target,'operations':step['operations']}
                or step_digest != digest(step)):
            return None
        known = _versions().get(str(end))
        if known is not None and known != target:
            return None
        revision, anchor = end, target
    return anchor if revision == actual else None


def schema_is_compatible(conn, schema, required, compatibility):
    """Exact clean revision, or a clean expansion with a complete compatible receipt."""
    identifier(schema)
    state = _state(conn, schema)
    if state is None or state[1] != 0 or state[0] < required:
        return False
    # Preserve exact-version offline admission without a receipt. When an online
    # receipt exists, even the new binary must agree with its recorded digest.
    return ((state[0] == required and not _has_incoming_receipt(conn, schema, required))
            or _chain(conn, schema, required, state[0], compatibility) is not None)


def _ddl(conn, schema, op):
    prefix = identifier(schema) + '.'
    if op['kind'] == 'create_table':
        columns = [identifier(c['name']) + ' ' + c['type'] + ('' if c['nullable'] else ' NOT NULL')
                   for c in op['columns']]
        if op['primary_key']:
            columns.append('PRIMARY KEY (' + ','.join(map(identifier, op['primary_key'])) + ')')
        conn.execute('CREATE TABLE ' + prefix + identifier(op['name']) + ' (' + ','.join(columns) + ')')
    elif op['kind'] == 'add_column':
        conn.execute('ALTER TABLE ' + prefix + identifier(op['table']) + ' ADD COLUMN ' +
                     identifier(op['name']) + ' ' + op['type'])
    else:
        conn.execute('CREATE ' + ('UNIQUE ' if op['unique'] else '') + 'INDEX ' + identifier(op['name']) +
                     ' ON ' + prefix + identifier(op['table']) + ' (' + ','.join(map(identifier, op['columns'])) + ')')


def apply_plan(conn, schema, value, *, before_apply=None):
    """One bounded transaction on an idle connection; never retries or starts clients."""
    import psycopg
    from psycopg.pq import TransactionStatus
    from psycopg.types.json import Jsonb

    # Own a copy: callbacks/callers cannot change the approved operations mid-flight.
    value = json.loads(json.dumps(validate_plan(value)))
    identifier(schema)
    support = schema_support()
    require(value['compatibility'] == support['compatibility'], 'Online migration compatibility differs from this runtime.')
    versions = _versions()
    for step in value['steps']:
        require(all(versions.get(str(step[revision])) in (None, step[signature]) for revision, signature in
                    (('from_revision', 'source_digest'), ('to_revision', 'target_digest'))),
                'Online migration plan differs from a locally known schema digest.')
    require(conn.info.transaction_status == TransactionStatus.IDLE, 'Online migration requires an idle dedicated connection.')
    table = identifier(schema) + '.' + identifier(LEDGER)
    deadline = time.monotonic() + TRANSACTION_TIMEOUT_MS / 1000

    def budget():
        require(time.monotonic() < deadline, 'Online migration exceeded its transaction budget.')

    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout = '10s'")
            if conn.info.server_version >= 170000:
                conn.execute(f"SET LOCAL transaction_timeout = '{TRANSACTION_TIMEOUT_MS}ms'")
            require(conn.execute('SELECT pg_try_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)',
                                 (schema,)).fetchone()[0], 'Offline migration owns the schema; online migration did not start.')
            require(conn.execute(f'SELECT pg_try_advisory_xact_lock({MIGRATION_LOCK},%s::regnamespace::oid::int)',
                                 (schema,)).fetchone()[0], 'Another online migration owns the schema; retry only after inspection.')
            state = _state(conn, schema, for_update=True)
            require(state is not None and state[1] == 0 and state[0] in
                    {value['from_revision'], value['to_revision']}, 'Online migration requires its exact clean source or committed target.')
            anchor = _chain(conn, schema, support['required'], state[0], support['compatibility'])
            # A newer candidate can migrate from a known earlier schema; installed
            # bridge execution instead follows its receipts when the DB is newer.
            if state[0] < support['required']:
                anchor = _versions().get(str(state[0]))
            require(anchor is not None, 'Online migration source has no verified schema digest.')
            if state[0] == value['to_revision'] and value['steps']:
                require(conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0],
                        'Online migration target has no committed receipt.')
                rows = conn.execute(f'''SELECT from_revision,to_revision,compatibility,
                    source_digest,target_digest,step_digest,plan_digest,step FROM {table}
                    WHERE from_revision>=%s AND to_revision<=%s ORDER BY from_revision''',
                    (value['from_revision'], value['to_revision'])).fetchall()
                require(rows == [(s['from_revision'],s['to_revision'],s['compatibility'],s['source_digest'],
                                  s['target_digest'],digest(s),value['digest'],s) for s in value['steps']],
                        'Committed online migration differs from this plan.')
            elif value['steps']:
                require(anchor == value['steps'][0]['source_digest'], 'Online migration source digest changed.')
            if before_apply is not None:
                budget()
                before_apply()
            budget()
            status = 'already_applied'
            if state[0] != value['to_revision']:
                conn.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                    from_revision BIGINT PRIMARY KEY, to_revision BIGINT UNIQUE NOT NULL,
                    compatibility BIGINT NOT NULL, source_digest TEXT NOT NULL,
                    target_digest TEXT NOT NULL, step_digest TEXT NOT NULL,
                    plan_digest TEXT NOT NULL, step JSONB NOT NULL)''')
                for step in value['steps']:
                    for op in step['operations']:
                        budget()
                        _ddl(conn, schema, op)
                    budget()
                    conn.execute(f'INSERT INTO {table} VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                                 (step['from_revision'],step['to_revision'],step['compatibility'],step['source_digest'],
                                  step['target_digest'],digest(step),value['digest'],Jsonb(step)))
                budget()
                conn.execute(f'UPDATE {identifier(schema)}.schema_state SET revision=%s WHERE id=1', (value['to_revision'],))
                status = 'applied'
        return {'status':status,'from_revision':value['from_revision'],
                'to_revision':value['to_revision'],'digest':value['digest']}
    except psycopg.Error:
        raise DatabaseError('Online migration failed or result is uncertain; inspect committed receipts before retrying.') from None
