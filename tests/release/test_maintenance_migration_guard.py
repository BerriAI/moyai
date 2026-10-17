"""A maintenance guard runs under exclusive ownership, before any schema write."""
from contextlib import closing
from cryptography.fernet import Fernet
import psycopg
from psycopg import sql
import pytest

from app.config import Settings
from app.database import DatabaseError, PostgresDatabase
from app.db import Store
from app import schema_migrations
from postgres_fixture import target  # noqa: F401


@pytest.fixture
def settings(tmp_path, target):
    url, schema = target
    return Settings(_env_file=None, data_dir=tmp_path, moyai_database_url=url,
        moyai_database_schema=schema, moyai_database_initialize=True,
        encryption_key=Fernet.generate_key().decode(), session_secret='synthetic-migration-key')


def tables(connection, schema):
    return connection.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename',
                              (schema,)).fetchall()


def holds_exclusive(connection, schema):
    return connection.execute('''SELECT mode FROM pg_locks WHERE locktype='advisory'
        AND classid=726940 AND objid=%s::regnamespace::oid AND objsubid=2
        AND pid=pg_backend_pid() AND granted''', (schema,)).fetchall() == [('ExclusiveLock',)]


@pytest.mark.parametrize('existing', [False, True])
def test_rejected_guard_leaves_schema_receipt_and_ddl_untouched_and_releases_lock(settings, existing):
    url, schema = settings.moyai_database_url, settings.moyai_database_schema
    with psycopg.connect(url, autocommit=True) as inspector:
        inspector.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        if existing:
            inspector.execute('CREATE TABLE runs(id text primary key)')
            inspector.execute('CREATE TABLE schema_state(id integer primary key,revision integer,dirty integer)')
            inspector.execute('INSERT INTO schema_state VALUES(1,18,0)')
        before = tables(inspector, schema)
        calls = []
        def reject(owner, observed_schema):
            calls.append(observed_schema)
            assert observed_schema == schema and holds_exclusive(owner, schema)
            # The same guard connection sees no migration marker/DDL yet.
            assert tables(owner, schema) == before
            assert inspector.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                                     (schema,)).fetchone() == (False,)
            if existing:
                assert owner.execute(sql.SQL('SELECT revision,dirty FROM {}.schema_state WHERE id=1')
                                     .format(sql.Identifier(schema))).fetchone() == (18, 0)
            raise DatabaseError('Synthetic maintenance guard rejected work')
        with pytest.raises(DatabaseError, match='guard rejected'):
            schema_migrations.migrate(settings, before_migrate=reject)
        assert calls == [schema] and tables(inspector, schema) == before
        if existing:
            assert inspector.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone() == (18, 0)
        # Constructor cleanup releases the failed migrator's exclusive lock.
        assert inspector.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                                 (schema,)).fetchone() == (True,)


def test_active_runtime_owner_prevents_guard_from_running(settings):
    url, schema = settings.moyai_database_url, settings.moyai_database_schema
    with psycopg.connect(url, autocommit=True) as runtime:
        runtime.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)', (schema,))
        def rejected_call(*args): pytest.fail('Guard ran before exclusive runtime ownership')
        with pytest.raises(DatabaseError, match='Another Moyai instance'):
            schema_migrations.migrate(settings, before_migrate=rejected_call)
        assert tables(runtime, schema) == []


def test_allowed_guard_precedes_dirty_marker_and_normal_migration_completes(settings):
    url, schema = settings.moyai_database_url, settings.moyai_database_schema
    calls = []
    with psycopg.connect(url, autocommit=True) as inspector:
        def allow(owner, observed_schema):
            assert observed_schema == schema and holds_exclusive(owner, schema)
            assert tables(owner, schema) == []
            assert inspector.execute('SELECT pg_try_advisory_lock_shared(726940,%s::regnamespace::oid::int)',
                                     (schema,)).fetchone() == (False,)
            calls.append('checked')
        assert schema_migrations.migrate(settings, before_migrate=allow) == {
            'schema_revision': schema_migrations.SCHEMA_REVISION, 'status': 'ready'}
        assert calls == ['checked']
        inspector.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        assert inspector.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone() == (
            schema_migrations.SCHEMA_REVISION, 0)
        assert inspector.execute('SELECT * FROM runtime_policy').fetchall() == []


def test_private_guard_diagnostic_does_not_escape_constructor(settings):
    def reject(*args): raise RuntimeError('private-guard-diagnostic')
    with pytest.raises(DatabaseError, match='Postgres initialization failed') as error:
        schema_migrations.migrate(settings, before_migrate=reject)
    assert 'private-guard-diagnostic' not in str(error.value)


@pytest.mark.parametrize('late_work', ['lease', 'model_request', None])
def test_actual_locked_guard_rechecks_work_after_outer_probe_before_any_migration(settings, monkeypatch, late_work):
    from scripts.release.maintenance_migration import check_locked
    from scripts.release.maintenance_probe import drain_counts, snapshot

    schema_migrations.migrate(settings)
    url, schema = settings.moyai_database_url, settings.moyai_database_schema
    with closing(Store(settings.data_dir, database_url=url, database_schema=schema, schema_mode='verify')) as store:
        run = store.create_run('Preserve the saved request across maintenance', '', 'demo', [], chat_enabled=True)
    expected = {'schema': schema_migrations.SCHEMA_REVISION - 1, 'policy': 'f' * 64,
                'contracts': {role: char * 64 for role, char in zip(('api', 'worker', 'broker', 'coordinator'), 'abcd')}}
    with psycopg.connect(url, autocommit=True) as writer:
        writer.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        writer.execute('UPDATE schema_state SET revision=%s', (expected['schema'],))
        writer.execute('INSERT INTO runtime_policy VALUES(1,%s)', (expected['policy'],))
        for role, fingerprint in expected['contracts'].items():
            writer.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s)').format(sql.Identifier('runtime_' + role + '_policy')),
                           (expected['policy'], fingerprint))
        # The outer probe is genuinely clean. Work arriving after this observation
        # must be caught by the same production helper inside exclusive ownership.
        with psycopg.connect(url) as reader:
            outer = snapshot(reader, schema)
        assert outer['owners'] == [] and outer['live_leases'] == outer['model_requests'] == 0
        if late_work == 'lease':
            writer.execute("INSERT INTO runtime_leases VALUES('late-work','synthetic',clock_timestamp()+interval '1 hour')")
        elif late_work == 'model_request':
            writer.execute("INSERT INTO model_requests(id,key_hash,run_id,model,created_at) VALUES('late-model','synthetic',%s,'fixture-model','synthetic-time')", (run['id'],))
        calls = []
        original = schema_migrations.prepare_schema
        def prepare(*args, **kwargs):
            calls.append('prepare')
            return original(*args, **kwargs)
        monkeypatch.setattr(schema_migrations, 'prepare_schema', prepare)
        def guard(owner, observed_schema):
            assert holds_exclusive(owner, schema)
            check_locked(owner, observed_schema, expected, drain_counts=drain_counts)
        if late_work:
            with pytest.raises(DatabaseError, match='Postgres initialization failed'):
                schema_migrations.migrate(settings, before_migrate=guard)
            assert calls == []
            assert writer.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone() == (expected['schema'], 0)
        else:
            assert schema_migrations.migrate(settings, before_migrate=guard) == {
                'schema_revision': schema_migrations.SCHEMA_REVISION, 'status': 'ready'}
            assert calls == ['prepare']
            assert writer.execute('SELECT revision,dirty FROM schema_state WHERE id=1').fetchone() == (schema_migrations.SCHEMA_REVISION, 0)
        assert writer.execute('SELECT fingerprint FROM runtime_policy WHERE id=1').fetchone() == (expected['policy'],)
        assert writer.execute('SELECT prompt FROM runs WHERE id=%s', (run['id'],)).fetchone() == (run['prompt'],)


def test_billing_observer_must_identify_the_actual_exclusive_transaction(settings, monkeypatch):
    from scripts.release.maintenance_migration import check_locked
    from scripts.release.maintenance_probe import drain_counts

    monkeypatch.delenv('MOYAI_NATIVE_SCHEMA', raising=False)
    schema_migrations.migrate(settings)
    url, schema = settings.moyai_database_url, settings.moyai_database_schema
    expected = {'schema': schema_migrations.SCHEMA_REVISION, 'policy': 'f' * 64,
                'contracts': {role: char * 64 for role, char in
                              zip(('api', 'worker', 'broker', 'coordinator'), 'abcd')}}
    with psycopg.connect(url, autocommit=True) as owner, psycopg.connect(url, autocommit=True) as observer:
        owner.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        owner.execute('INSERT INTO runtime_policy VALUES(1,%s)', (expected['policy'],))
        for role, fingerprint in expected['contracts'].items():
            owner.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s)')
                          .format(sql.Identifier('runtime_' + role + '_policy')),
                          (expected['policy'], fingerprint))
        with owner.transaction():
            owner.execute('SELECT pg_advisory_xact_lock(726940,%s::regnamespace::oid::int)', (schema,))
            check_locked(observer, schema, expected, drain_counts=drain_counts, owner_pid=owner.info.backend_pid)
            for wrong in (observer.info.backend_pid, owner.info.backend_pid + 1, True, 0):
                with pytest.raises(ValueError):
                    check_locked(observer, schema, expected, drain_counts=drain_counts, owner_pid=wrong)
        with pytest.raises(ValueError):
            check_locked(observer, schema, expected, drain_counts=drain_counts, owner_pid=owner.info.backend_pid)


@pytest.mark.parametrize('constructor', ['store', 'database'])
@pytest.mark.parametrize('mode,instance,role,guard', [
    ('auto', True, 'standalone', lambda *args: None),
    ('verify', True, 'standalone', lambda *args: None),
    ('migrate', False, 'standalone', lambda *args: None),
    ('migrate', True, 'worker', lambda *args: None),
    ('migrate', True, 'standalone', False),
])
def test_guard_cannot_be_attached_to_runtime_startup(tmp_path, monkeypatch, constructor, mode, instance, role, guard):
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: pytest.fail('Invalid guard reached PostgreSQL'))
    options = dict(schema_mode=mode, application_instance=instance, runtime_role=role, migration_guard=guard)
    with pytest.raises(ValueError):
        if constructor == 'store': Store(tmp_path, database_url='private-dsn', **options)
        else: PostgresDatabase('private-dsn', 'synthetic', **options)


def test_sqlite_store_never_silently_discards_a_migration_guard(tmp_path):
    with pytest.raises(ValueError, match='PostgreSQL migration ownership'):
        Store(tmp_path, migration_guard=lambda *args: None)
    assert not (tmp_path / 'workspace.db').exists()
