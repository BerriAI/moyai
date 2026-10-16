"""Real isolated PostgreSQL observations and lock guards; no production access."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from scripts.release import native_probe, native_migration, maintenance_migration
from scripts.release.maintenance_probe import drain_counts
from postgres_fixture import target  # noqa: F401


@pytest.fixture
def native_db(target, monkeypatch):
    url, _ = target
    # Keep generated index identifiers below PostgreSQL's truncation limit.
    # Long-name collisions have their own deterministic preflight regression.
    schema = 'n' + uuid4().hex[:8]
    monkeypatch.setenv('MOYAI_NATIVE_SCHEMA', schema)
    monkeypatch.delenv('MOYAI_DATABASE_SCHEMA', raising=False)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            yield connection, schema
        finally:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_empty_native_destination_and_unreceipted_data_are_distinct(native_db):
    connection, schema = native_db
    assert native_probe.inventory(connection) == {
        'schema': schema, 'state': 'empty', 'app': {}, 'harness': None}
    connection.execute(sql.SQL('CREATE TABLE {}.unexpected(id integer)').format(sql.Identifier(schema)))
    with pytest.raises(ValueError):
        native_probe.inventory(connection)


def test_native_counts_include_unknown_effects_requests_and_queued_or_waiting_turns(native_db):
    connection, schema = native_db
    for table, column in (('moyai_turns', 'status'), ('moyai_effects', 'state'),
                          ('moyai_product_inference_requests', 'status'), ('moyai_inference_usage', 'status')):
        connection.execute(sql.SQL('CREATE TABLE {}.{}({} text)').format(
            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(column)))
    def insert(table, values):
        for value in values:
            connection.execute(sql.SQL('INSERT INTO {}.{} VALUES(%s)').format(
                sql.Identifier(schema), sql.Identifier(table)), (value,))
    insert('moyai_turns', ['queued', 'waiting', 'running', 'starting', 'saving', 'steering', 'signal_sent',
                           'accepted', 'unknown', None, 'completed', 'failed', 'stopped', 'resumed'])
    insert('moyai_effects', ['pending', 'unknown', None, 'confirmed', 'not_dispatched'])
    insert('moyai_product_inference_requests', ['dispatching', 'unknown', 'completed', 'failed', 'not_dispatched'])
    insert('moyai_inference_usage', ['pending', 'running', 'unknown', None, 'completed', 'failed'])
    with connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        assert native_probe.drain_counts(connection) == {
            'native_runs': 9, 'native_queued': 1, 'native_parked': 0, 'native_effects': 3, 'native_requests': 6,
            'native_automations': 0, 'native_events': 0, 'native_automation_history': 0}
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute(sql.SQL('DELETE FROM {}.moyai_turns').format(sql.Identifier(schema)))


def test_absent_optional_native_tables_are_zero_without_guessing_a_schema(native_db, monkeypatch):
    connection, schema = native_db
    assert native_probe.drain_counts(connection) == dict.fromkeys(native_probe.COUNTS + native_probe.CORE_COUNTS, 0)
    monkeypatch.delenv('MOYAI_NATIVE_SCHEMA')
    assert native_probe.inventory(connection) is None
    monkeypatch.setenv('MOYAI_CORE', 'pydantic')
    with pytest.raises(ValueError):
        native_probe.drain_counts(connection)


@pytest.mark.parametrize('state,sync,generation,synced,blocked', [
    ('paused', 'ready', 3, 3, 0), ('deleted', 'ready', 3, 3, 0),
    ('active', 'ready', 3, 3, 1), ('paused', 'pending', 3, 3, 1),
    ('deleted', 'blocked', 3, 3, 1), ('paused', 'ready', 3, 2, 1),
    ('unknown', 'ready', 3, 3, 1), (None, 'ready', 3, 3, 1),
    ('paused', None, 3, 3, 1), ('paused', 'ready', None, None, 1),
    ('paused', 'ready', 0, 0, 1), ('paused', 'ready', 3, None, 1),
])
def test_core_retirement_requires_confirmed_native_automation_pause(native_db, state, sync, generation, synced, blocked):
    connection, schema = native_db
    connection.execute(sql.SQL('''CREATE TABLE {}.moyai_product_automation_bindings(
        state text,sync_state text,generation integer,synced_generation integer)''').format(sql.Identifier(schema)))
    connection.execute(sql.SQL('INSERT INTO {}.moyai_product_automation_bindings VALUES(%s,%s,%s,%s)')
                       .format(sql.Identifier(schema)), (state, sync, generation, synced))
    with connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        observed = native_probe.drain_counts(connection)
        assert observed['native_automations'] == blocked
        assert observed['native_automation_history'] == 1
        assert all(observed[key] == 0 for key in native_probe.COUNTS)


@pytest.mark.parametrize('fault', [None, 'generation', 'workflow', 'phase', 'resumed', 'unpaused', 'waiting',
                                 'unknown', 'missing_run', 'missing_boundary'])
def test_only_current_paused_sdk_receipts_exclude_executing_turns(native_db, fault):
    connection, schema = native_db
    connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    connection.execute('''CREATE TABLE moyai_turns(id text, workflow_id text, status text,
        result_accepted boolean, conversation text)''')
    connection.execute('CREATE TABLE moyai_product_drain_state(id integer, generation bigint, paused boolean)')
    connection.execute('''CREATE TABLE moyai_product_drain_receipts(
        turn_id text, workflow_id text, generation bigint, phase text, resumed_generation bigint,
        workflow_run_id text, boundary text,
        PRIMARY KEY(turn_id,generation))''')
    status = fault if fault in {'waiting', 'unknown'} else 'running'
    connection.execute("INSERT INTO moyai_turns(id,workflow_id,status) VALUES('active','workflow-active',%s),('queued','workflow-queued','queued')",
                       (status,))
    connection.execute('INSERT INTO moyai_product_drain_state VALUES(1,4,%s)', (fault != 'unpaused',))
    connection.execute('INSERT INTO moyai_product_drain_receipts VALUES(%s,%s,%s,%s,%s,%s,%s)',
        ('active', 'other-workflow' if fault == 'workflow' else 'workflow-active',
         3 if fault == 'generation' else 4, 'blocked' if fault == 'phase' else 'parked',
         5 if fault == 'resumed' else None, '' if fault == 'missing_run' else str(uuid4()),
         '' if fault == 'missing_boundary' else 'sdk-node'))
    observed = native_probe.drain_counts(connection)
    assert observed['native_runs'] == (0 if fault is None else 1)
    assert observed['native_parked'] == (1 if fault is None else 0)
    assert observed['native_queued'] == 1


@pytest.mark.parametrize('fault', [None, 'boundary', 'not_accepted', 'missing_conversation', 'resumed', 'generation'])
def test_deferred_waiting_needs_the_qualified_temporal_receipt_and_saved_acceptance(native_db, fault):
    connection, schema = native_db
    connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
    connection.execute('''CREATE TABLE moyai_turns(id text, workflow_id text, status text,
        result_accepted boolean, conversation text)''')
    connection.execute('CREATE TABLE moyai_product_drain_state(id integer, generation bigint, paused boolean)')
    connection.execute('''CREATE TABLE moyai_product_drain_receipts(turn_id text, workflow_id text,
        generation bigint, phase text, resumed_generation bigint, workflow_run_id text, boundary text)''')
    connection.execute("INSERT INTO moyai_turns VALUES('wait','workflow-wait','waiting',%s,%s)",
                       (fault != 'not_accepted', None if fault == 'missing_conversation' else 'saved-conversation'))
    connection.execute('INSERT INTO moyai_product_drain_state VALUES(1,3,true)')
    connection.execute('INSERT INTO moyai_product_drain_receipts VALUES(%s,%s,%s,%s,%s,%s,%s)',
        ('wait', 'workflow-wait', 2 if fault == 'generation' else 3, 'parked',
         4 if fault == 'resumed' else None, str(uuid4()),
         'sdk-node' if fault == 'boundary' else 'deferred_waiting'))
    observed = native_probe.drain_counts(connection)
    assert observed['native_runs'] == (0 if fault is None else 1)
    assert observed['native_parked'] == (1 if fault is None else 0)


@pytest.mark.parametrize('schema', ['public', 'information_schema', 'pg_private', 'bad;select 1'])
def test_schema_identifiers_are_checked_before_any_query(monkeypatch, schema):
    monkeypatch.setenv('MOYAI_NATIVE_SCHEMA', schema)
    with pytest.raises(ValueError):
        native_probe.inventory(None)


def test_real_harness_receipt_matches_legacy_interpreter_shape_and_rejects_drift(native_db, tmp_path):
    connection, schema = native_db
    root = Path(__file__).resolve().parents[2]
    interpreter = Path(native_migration.native_python(root))
    if not interpreter.exists():
        pytest.skip('Install the workspace interpreter for SDK receipt comparison.')
    # Real SDK schema operations against the isolated database. No Runtime,
    # model, Temporal client, provider, filesystem blob, or application starts.
    code = '''
import asyncio, json, os
from moyai.settings import Settings
from moyai.stores import NativeStores
from moyai.product_schema import migrate_product_schema
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
    settings = Settings(_env_file=None, database_url=os.environ["TEST_URL"],
        database_schema=os.environ["TEST_SCHEMA"], data_dir=os.environ["TEST_DATA"],
        native_schema_mode="verify")
    engine = create_async_engine(settings.sqlalchemy_url)
    stores = NativeStores(settings)
    try:
        await migrate_product_schema(engine, schema=settings.database_schema)
        await stores.start(provision=True)
    finally:
        await stores.close()
        await engine.dispose()
asyncio.run(main())
'''
    result = subprocess.run([str(interpreter), '-c', code], cwd=root,
        env={'PATH': os.defpath, 'PYTHONPATH': str(root), 'TEST_URL': os.environ['MOYAI_TEST_POSTGRES_URL'],
             'TEST_SCHEMA': schema, 'TEST_DATA': str(tmp_path)}, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    observed = native_probe.inventory(connection)
    assert observed['state'] == 'receipted'
    assert observed['harness'] == {'sdk_version': '2.55.0', 'harness_version': '0.55.0'}
    assert native_probe.drain_counts(connection) == dict.fromkeys(native_probe.COUNTS + native_probe.CORE_COUNTS, 0)
    connection.execute(sql.SQL('ALTER TABLE {}.plan_items ADD COLUMN unexpected text').format(sql.Identifier(schema)))
    with pytest.raises(ValueError):
        native_probe.inventory(connection)


def test_actual_postgres_shortened_index_collision_is_rejected_by_pure_candidate_check():
    root = Path(__file__).resolve().parents[2]
    interpreter = Path(native_migration.native_python(root))
    if not interpreter.is_file():
        pytest.skip('Install the locked native interpreter for actual SQLAlchemy name rendering.')
    source = native_migration.SCHEMA_IDENTIFIERS_SOURCE + '''
import json
from moyai.product_schema import application_metadata
results = []
for schema in ("release_native", "native_cutover_f157df6fc475472ca0b3688f491c803f"):
    try:
        validate_schema_identifiers(application_metadata(schema))
        results.append("valid")
    except ValueError:
        results.append("collision")
print(json.dumps(results))
'''
    result = subprocess.run([str(interpreter), '-c', source], cwd=root,
        env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout) == ['valid', 'collision']


@pytest.fixture
def guard(native_db, target, monkeypatch):
    connection, schema = native_db
    url, _ = target
    # Independent canonical schema with just the existing ownership contract
    # needed by check_locked; this is a guard test, not an app migration test.
    source = 'release_source_' + uuid4().hex
    connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(source)))
    connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(source)))
    connection.execute('CREATE TABLE schema_state(id integer primary key,revision integer,dirty integer)')
    connection.execute('INSERT INTO schema_state VALUES(1,18,0)')
    connection.execute('CREATE TABLE runtime_policy(id integer primary key,fingerprint text)')
    connection.execute('INSERT INTO runtime_policy VALUES(1,%s)', ('a' * 64,))
    contracts = {role: 'b' * 64 for role in ('api', 'coordinator', 'worker', 'broker')}
    for role, digest in contracts.items():
        connection.execute(sql.SQL('CREATE TABLE {}(id integer primary key,cluster_fingerprint text,fingerprint text)')
                           .format(sql.Identifier('runtime_' + role + '_policy')))
        connection.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s)')
                           .format(sql.Identifier('runtime_' + role + '_policy')), ('a' * 64, digest))
    connection.execute('CREATE TABLE runs(id text,deleted_at text,status text)')
    connection.execute('CREATE TABLE durable_sessions(run_id text,state text)')
    connection.execute('CREATE TABLE model_requests(status text)')
    connection.execute('CREATE TABLE runtime_leases(expires_at timestamptz)')
    monkeypatch.setenv('MOYAI_DATABASE_SCHEMA', source)
    expected = {'schema': 18, 'source_schema': 18, 'sha': 'a' * 40, 'policy': 'a' * 64,
                'contracts': contracts, 'native_required': True, 'core_switch': True,
                **{key: [] for key in maintenance_migration.INVENTORIES}}
    state = {**deepcopy(expected), 'ok': True, 'ready': True, 'staged': True, 'identity': None,
             **dict.fromkeys((*maintenance_migration.COUNTS, *native_probe.COUNTS,
                              *native_probe.CORE_COUNTS, 'legacy_core_runs',
                              'legacy_automations', 'legacy_events'), 0)}
    service = {'role': 'coordinator', 'staged': True, 'drain': False, 'sha': expected['sha']}
    manifest = {'schema': schema, 'app': {'v1': '1' * 64},
                'harness': {'sdk_version': '2.55.0', 'harness_version': '0.55.0'}}
    observed = {'schema': schema, 'state': 'empty', 'app': {}, 'harness': None}
    plan = native_probe.migration_plan(deepcopy(observed), manifest)
    options = {'service': service, 'expected': expected, 'plan': plan}
    monkeypatch.setenv('MOYAI_CORE', 'pydantic')
    monkeypatch.setattr('app.config.Settings', lambda **kwargs: SimpleNamespace(
        # Connection.info.dsn omits passwords; reconnect with the fixture's
        # original credentials on password-authenticated PostgreSQL, as in CI.
        moyai_database_url=url, moyai_database_schema=source))
    calls = []
    def command(args):
        assert connection.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                                  (source,)).fetchone() == (False,)
        calls.append(args)
        observed.clear()
        observed.update(manifest, state='receipted')
    monkeypatch.setattr(native_migration, 'native_command', command)
    def execute():
        return native_migration.execute(options, probe_fn=lambda _: state, counts_fn=drain_counts,
            inventory_fn=lambda _: deepcopy(observed), plan_fn=native_probe.migration_plan,
            check_locked_fn=maintenance_migration.check_locked)
    try:
        yield connection, source, execute, calls, state
    finally:
        connection.execute('SELECT pg_advisory_unlock_all()')
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(source)))


def test_exclusive_canonical_owner_lock_spans_app_and_harness_provision_and_verify(guard):
    connection, source, execute, calls, _ = guard
    assert execute()['status'] == 'verified'
    assert calls == [['provision', '--apply'], ['verify']]
    assert connection.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                              (source,)).fetchone() == (True,)


def test_late_source_owner_blocks_native_provision_before_any_write(guard):
    connection, source, execute, calls, _ = guard
    connection.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (source,))
    with pytest.raises(ValueError, match='source owner'):
        execute()
    assert not calls


def test_locked_native_count_recheck_catches_work_after_unlocked_snapshot(guard, monkeypatch):
    connection, _, execute, calls, _ = guard
    schema = os.environ['MOYAI_NATIVE_SCHEMA']
    connection.execute(sql.SQL('CREATE TABLE {}.moyai_effects(state text)').format(sql.Identifier(schema)))
    connection.execute(sql.SQL("INSERT INTO {}.moyai_effects VALUES('pending')").format(sql.Identifier(schema)))
    with pytest.raises(ValueError, match='precondition'):
        execute()
    assert not calls


@pytest.mark.parametrize('state', ['active', 'paused', 'deleted'])
def test_locked_core_switch_rejects_unqualified_automation_control_history_even_without_turns(guard, state):
    connection, _, execute, calls, _ = guard
    schema = os.environ['MOYAI_NATIVE_SCHEMA']
    connection.execute(sql.SQL('''CREATE TABLE {}.moyai_product_automation_bindings(
        state text,sync_state text,generation integer,synced_generation integer)''').format(sql.Identifier(schema)))
    connection.execute(sql.SQL(
        "INSERT INTO {}.moyai_product_automation_bindings VALUES(%s,'ready',1,1)"
    ).format(sql.Identifier(schema)), (state,))
    with pytest.raises(ValueError, match='precondition'):
        execute()
    assert not calls


@pytest.mark.parametrize('status,blocked', [
    ('native_pending', True), ('native_dispatched', True), ('native_unknown', True),
    ('admitted', False), ('skipped', False), ('ignored', False), ('blocked', False),
])
def test_core_switch_accounts_for_native_canonical_event_inbox_before_migration(guard, status, blocked):
    connection, _, execute, calls, _ = guard
    connection.execute('CREATE TABLE automation_events(status text)')
    connection.execute('INSERT INTO automation_events VALUES(%s)', (status,))
    observed = native_probe.drain_counts(connection)
    assert observed['native_events'] == int(blocked)
    if blocked:
        with pytest.raises(ValueError, match='precondition'):
            execute()
        assert not calls
    else:
        assert execute()['status'] == 'verified'


@pytest.mark.parametrize('fault', [None, 'enabled', 'revision', 'version', 'error', 'event'])
def test_source_automation_pause_and_inbox_are_rechecked_under_exclusive_guard(guard, fault):
    from app.automation_definitions import SCHEDULE_VERSION
    from scripts.release.maintenance_probe import legacy_automation_counts
    connection, _, execute, calls, _ = guard
    connection.execute('''CREATE TABLE automations(
        paused integer,revision integer,synced_revision integer,synced_schedule_version integer,sync_error text)''')
    connection.execute('INSERT INTO automations VALUES(%s,3,%s,%s,%s)',
        (0 if fault == 'enabled' else 1, 2 if fault == 'revision' else 3,
         SCHEDULE_VERSION + 1 if fault == 'version' else SCHEDULE_VERSION,
         'unconfirmed' if fault == 'error' else ''))
    connection.execute('CREATE TABLE automation_events(status text)')
    if fault == 'event':
        connection.execute("INSERT INTO automation_events VALUES('pending')")
    observed = legacy_automation_counts(connection)
    assert observed['legacy_automations'] == int(fault in {'enabled', 'revision', 'version', 'error'})
    assert observed['legacy_events'] == int(fault == 'event')
    if fault:
        with pytest.raises(ValueError, match='precondition'):
            execute()
        assert not calls
    else:
        assert execute()['status'] == 'verified'


def test_failed_native_command_releases_guard_without_retry(guard, monkeypatch):
    connection, source, execute, calls, _ = guard
    def unknown(args):
        calls.append(args)
        raise TimeoutError('Synthetic ambiguous native command')
    monkeypatch.setattr(native_migration, 'native_command', unknown)
    with pytest.raises(TimeoutError):
        execute()
    assert calls == [['provision', '--apply']]
    assert connection.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                              (source,)).fetchone() == (True,)


def test_actual_product_provision_and_verify_cli_under_canonical_exclusive_lock(native_db, tmp_path):
    """Real migration commands on isolated DB; still not deployed-app E2E."""
    from cryptography.fernet import Fernet
    from app.config import Settings
    from app.schema_migrations import SCHEMA_REVISION, migrate

    connection, native_schema = native_db
    root = Path(__file__).resolve().parents[2]
    interpreter = Path(native_migration.native_python(root))
    if not interpreter.exists():
        pytest.skip('Install the workspace environment for CLI integration.')
    source = 'moyai_release_cli_' + uuid4().hex
    url = os.environ['MOYAI_TEST_POSTGRES_URL']
    key = Fernet.generate_key().decode()
    connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(source)))
    environment = {
        'PATH': os.defpath, 'PYTHONPATH': str(root), 'DATA_DIR': str(tmp_path),
        'MOYAI_CORE': 'pydantic', 'MOYAI_PRODUCT_MODE': 'existing',
        'MOYAI_RUNTIME_ROLE': 'coordinator', 'MOYAI_DATABASE_URL': url,
        'MOYAI_DATABASE_SCHEMA': source, 'MOYAI_NATIVE_SCHEMA': native_schema,
        'MOYAI_SCHEMA_MODE': 'verify', 'MOYAI_DATABASE_INITIALIZE': 'false',
        'MOYAI_SEPARATE_BROKER': 'true', 'MOYAI_COORDINATOR_HANDOFF': 'true',
        'RENDER_MIGRATION_STAGE': 'true', 'MAINTENANCE_DRAIN': 'false',
        'SESSION_SECRET': 'synthetic-source-session-secret', 'ENCRYPTION_KEY': key,
        'MOYAI_NATIVE_ENCRYPTION_KEY': 'synthetic-native-key-at-least-32-characters',
        'MOYAI_NATIVE_KEY_FORMAT': 'sha256-text-fernet-v1',
        'MOYAI_NATIVE_OBJECT_FORMAT': 'fernet-hmac-sha256-v1',
        'MOYAI_NATIVE_OBJECT_PREFIX': 'native-cli-test', 'OBJECT_STORAGE_PREFIX': 'source-cli-test',
        'OBJECT_STORAGE_BUCKET': 'synthetic-unused', 'OBJECT_STORAGE_ENDPOINT': 'http://127.0.0.1:1',
        'TEMPORAL_ENABLED': 'true', 'TEMPORAL_ADDRESS': '127.0.0.1:1', 'TEMPORAL_NAMESPACE': 'synthetic',
        'TEMPORAL_TASK_QUEUE': 'source-cli-test', 'MOYAI_NATIVE_TASK_QUEUE': 'native-cli-test-v1',
        'MOYAI_PUBLIC_URL': 'https://synthetic.invalid',
    }
    try:
        settings = Settings(_env_file=None, moyai_core='legacy', data_dir=tmp_path,
            moyai_database_url=url, moyai_database_schema=source, moyai_database_initialize=True,
            encryption_key=key, session_secret=environment['SESSION_SECRET'])
        migrate(settings)
        connection.execute('SELECT pg_advisory_lock(726940,%s::regnamespace::oid::int)', (source,))
        for command, app_receipt, harness_receipt in (
            (['provision', '--apply'], None, 'provisioned'), (['verify'], 'verified', 'verified'),
        ):
            result = subprocess.run([str(interpreter), '-m', 'moyai.product_start', *command],
                cwd=root, env=environment, capture_output=True, text=True, timeout=90)
            assert result.returncode == 0, f'{command}: {result.stderr}'
            receipt = json.loads(result.stdout)
            assert receipt['core'] == 'pydantic-v1' and receipt['native_schema'] == native_schema
            assert receipt['harness_schema'] == harness_receipt
            assert receipt['app_schema'] == app_receipt if app_receipt else bool(receipt['app_schema'])
        assert native_probe.inventory(connection)['state'] == 'receipted'
        assert connection.execute(sql.SQL('SELECT revision,dirty FROM {}.schema_state WHERE id=1')
                                  .format(sql.Identifier(source))).fetchone() == (SCHEMA_REVISION, 0)
        assert not list(tmp_path.rglob('*.db'))  # No SQLite fallback or agent bootstrap.
    finally:
        connection.execute('SELECT pg_advisory_unlock_all()')
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(source)))
