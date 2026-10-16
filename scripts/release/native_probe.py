"""Native receipt/count observations usable by the installed legacy interpreter.

No native SDK imports, schema writes, runtime startup, or customer content. The
caller owns the read-only snapshot and canonical schema search path.
"""
import hashlib
import json
import os
import re

COUNTS = ('native_runs', 'native_effects', 'native_requests')
CORE_COUNTS = ('native_queued', 'native_parked', 'native_automations', 'native_events',
               'native_automation_history')
HARNESS_TABLES = (
    'agent_memory', 'agent_memory_operations', 'agent_memory_metadata', 'plan_items',
    'step_persistence_runs', 'step_persistence_events', 'step_persistence_snapshots',
    'step_persistence_snapshot_keys', 'step_persistence_tool_effects', 'step_persistence_media',
)


def require(condition):
    if not condition:
        raise ValueError('Native release inventory is incomplete or incompatible.')


def configured_schema():
    schema = os.environ.get('MOYAI_NATIVE_SCHEMA', '')
    require(not schema or (re.fullmatch('[a-z][a-z0-9_]{0,62}', schema)
            and schema not in {'public', 'information_schema', os.environ.get('MOYAI_DATABASE_SCHEMA')}
            and not schema.startswith('pg_')))
    require(os.environ.get('MOYAI_CORE', 'legacy') != 'pydantic' or schema)
    return schema


def table_exists(connection, schema, table):
    return connection.execute('SELECT to_regclass(quote_ident(%s)||\'.\'||quote_ident(%s))',
                              (schema, table)).fetchone()[0] is not None


def drain_counts(connection, schema=None):
    from psycopg import sql
    schema = configured_schema() if schema is None else schema
    counts = dict.fromkeys(COUNTS + CORE_COUNTS, 0)
    if not schema:
        return counts
    source_schema = os.environ.get('MOYAI_DATABASE_SCHEMA')
    if source_schema and table_exists(connection, source_schema, 'automation_events'):
        # These canonical inbox rows belong to native Temporal workflows. The
        # legacy scheduler only consumes its own 'pending' state.
        counts['native_events'] = connection.execute(sql.SQL("""
            SELECT count(*) FROM {}.automation_events
            WHERE left(status,7)='native_'
        """).format(sql.Identifier(source_schema))).fetchone()[0]
    if table_exists(connection, schema, 'moyai_product_automation_bindings'):
        # Temporal schedules can create future native work without a Turn yet.
        # A saved pause intent alone is insufficient: reconciliation must have
        # confirmed the same generation at the provider. Same-core maintenance
        # preserves these definitions; retiring the core requires this zero.
        counts['native_automations'] = connection.execute(sql.SQL("""
            SELECT count(*) FROM {}.moyai_product_automation_bindings
            WHERE state IS NULL OR state NOT IN ('paused','deleted')
                OR sync_state IS DISTINCT FROM 'ready'
                OR generation IS NULL OR generation <= 0
                OR synced_generation IS DISTINCT FROM generation
        """).format(sql.Identifier(schema))).fetchone()[0]
        # The current automation protocol does not persist every Temporal
        # control workflow/run identity. Even an acknowledged schedule pause
        # cannot prove all earlier sync/tick/event workflows have finished.
        # Refuse core retirement after automation adoption until that producer
        # fence and exact workflow-drain receipt are qualified. This is a count
        # of binding history, never a guessed count of Temporal executions.
        counts['native_automation_history'] = connection.execute(sql.SQL(
            'SELECT count(*) FROM {}.moyai_product_automation_bindings'
        ).format(sql.Identifier(schema))).fetchone()[0]
    # Count unknown states conservatively. Queued native turns may survive a
    # same-core deployment, but a core switch must not strand or reinterpret them.
    inventories = (
        ('native_runs', 'moyai_turns', 'status', ('queued', 'completed', 'failed', 'stopped', 'resumed')),
        ('native_effects', 'moyai_effects', 'state', ('confirmed', 'not_dispatched')),
        ('native_requests', 'moyai_product_inference_requests', 'status',
         ('completed', 'failed', 'not_dispatched')),
        ('native_requests', 'moyai_inference_usage', 'status',
         ('completed', 'failed', 'not_dispatched')),
    )
    for key, table, column, finished in inventories:
        if table_exists(connection, schema, table):
            counts[key] += connection.execute(sql.SQL(
                'SELECT count(*) FROM {}.{} WHERE {} IS NULL OR {} != ALL(%s)'
            ).format(sql.Identifier(schema), sql.Identifier(table),
                     sql.Identifier(column), sql.Identifier(column)), (list(finished),)).fetchone()[0]
    if table_exists(connection, schema, 'moyai_turns'):
        counts['native_queued'] = connection.execute(sql.SQL(
            "SELECT count(*) FROM {}.moyai_turns WHERE status='queued'"
        ).format(sql.Identifier(schema))).fetchone()[0]
        if (table_exists(connection, schema, 'moyai_product_drain_state')
                and table_exists(connection, schema, 'moyai_product_drain_receipts')):
            counts['native_parked'] = connection.execute(sql.SQL("""
                SELECT count(*) FROM {schema}.moyai_turns t
                JOIN {schema}.moyai_product_drain_receipts r ON r.turn_id=t.id AND r.workflow_id=t.workflow_id
                JOIN {schema}.moyai_product_drain_state d ON d.id=1 AND d.generation=r.generation
                WHERE d.paused AND r.phase='parked' AND r.resumed_generation IS NULL
                AND r.boundary != ''
                AND r.workflow_run_id ~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$'
                AND (t.status IN ('starting','running','saving') OR
                    (t.status='waiting' AND r.boundary='deferred_waiting'
                     AND t.result_accepted IS TRUE AND t.conversation IS NOT NULL AND t.conversation != ''))
            """).format(schema=sql.Identifier(schema))).fetchone()[0]
            counts['native_runs'] -= counts['native_parked']
            require(counts['native_runs'] >= 0)
    return counts


def control_receipt(connection, schema=None):
    from psycopg import sql
    schema = configured_schema() if schema is None else schema
    if not schema or not table_exists(connection, schema, 'moyai_product_drain_state'):
        return None
    row = connection.execute(sql.SQL("""
        SELECT generation,paused,request_key,request_fingerprint,owner_pid,owner_started,source_policy
        FROM {}.moyai_product_drain_state WHERE id=1
    """).format(sql.Identifier(schema))).fetchone()
    require(row is not None)
    return {
        'protocol': 1, 'core': 'pydantic-v1', 'native_schema': schema,
        'task_queue': os.environ.get('MOYAI_NATIVE_TASK_QUEUE', ''),
        **dict(zip(('generation', 'paused', 'request_key', 'request_fingerprint'), row[:4])),
        'requester': {'pid': row[4], 'backend_start': row[5]}, 'source_policy': row[6],
    }


def harness_shape(connection, schema):
    # Same public PostgreSQL inventory as moyai.harness_schema.shape. Rows must
    # retain their names for the cross-interpreter JSON digest.
    queries = (
        ('''SELECT table_name,column_name,ordinal_position,data_type,is_nullable,column_default,is_identity
            FROM information_schema.columns WHERE table_schema=%s AND table_name=ANY(%s)
            ORDER BY table_name,ordinal_position''', (schema, list(HARNESS_TABLES))),
        ('''SELECT tablename,indexname,indexdef FROM pg_indexes
            WHERE schemaname=%s AND tablename=ANY(%s) ORDER BY tablename,indexname''',
         (schema, list(HARNESS_TABLES))),
        ('''SELECT sequencename,data_type,start_value,min_value,increment_by FROM pg_sequences
            WHERE schemaname=%s AND sequencename IN ('agent_memory_versions',
            'step_persistence_events_seq_seq','step_persistence_snapshots_seq_seq')
            ORDER BY sequencename''', (schema,)),
    )
    rows = []
    for query, args in queries:
        cursor = connection.execute(query, args)
        names = [column.name for column in cursor.description]
        rows.append([dict(zip(names, row)) for row in cursor.fetchall()])
    require({row['table_name'] for row in rows[0]} == set(HARNESS_TABLES) and len(rows[2]) == 3)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def inventory(connection, schema=None):
    from psycopg import sql
    schema = configured_schema() if schema is None else schema
    if not schema:
        return None
    tables = {row[0] for row in connection.execute(
        'SELECT relname FROM pg_class WHERE relnamespace=to_regnamespace(%s) AND relkind IN '
        "('r','p','v','m','f','S')", (schema,)).fetchall()}
    if not tables:
        return {'schema': schema, 'state': 'empty', 'app': {}, 'harness': None}
    # A partially provisioned/unknown destination requires explicit recovery.
    # Never bless it by blindly running create_all or replacing its receipt.
    require({'moyai_app_schema_versions', 'moyai_harness_schema'} <= tables)
    rows = connection.execute(sql.SQL('SELECT version,manifest FROM {}.moyai_app_schema_versions')
                              .format(sql.Identifier(schema))).fetchall()
    require(rows and all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', m) for v, m in rows)
            and len({v for v, _ in rows}) == len(rows))
    harness = connection.execute(sql.SQL(
        'SELECT sdk_version,harness_version,shape FROM {}.moyai_harness_schema WHERE id=1'
    ).format(sql.Identifier(schema))).fetchone()
    require(harness and harness[2] == harness_shape(connection, schema))
    return {'schema': schema, 'state': 'receipted', 'app': dict(rows),
            'harness': dict(zip(('sdk_version', 'harness_version'), harness[:2]))}


def migration_plan(observed, candidate):
    """Require a complete known receipt prefix; never infer codecs or versions."""
    require(isinstance(observed, dict) and observed.get('schema') == candidate['schema'])
    app = observed.get('app')
    require(isinstance(app, dict))
    if observed.get('state') == 'empty':
        require(app == {} and observed.get('harness') is None)
    else:
        require(observed.get('state') == 'receipted' and bool(app)
                and observed.get('harness') == candidate['harness'])
        versions = list(candidate['app'])
        require(set(app) == set(versions[:len(app)])
                and all(candidate['app'][version] == manifest for version, manifest in app.items()))
    return {'before': observed, 'target': candidate,
            'apply': observed['state'] == 'empty' or app != candidate['app']}
