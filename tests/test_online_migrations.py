"""Bounded additive migration and older-runtime admission on real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
import threading
import time
from uuid import uuid4

import psycopg
import pytest

from app import online_migrations as online, runtime_compatibility, schema_migrations
from app.config import Settings
from app.database import DatabaseError, PostgresDatabase, RUNTIME_SETTINGS
from app.db import Store

BASE = schema_migrations.SCHEMA_REVISION
COMPATIBILITY = schema_migrations.SCHEMA_COMPATIBILITY_REVISION


def expansion():
    step = {'from_revision': BASE, 'to_revision': BASE + 1, 'compatibility': COMPATIBILITY,
            'source_digest': online._versions()[str(BASE)], 'target_digest': 'a' * 64,
            'operations': [
                {'kind':'create_table','name':'new_jobs','columns':[
                    {'name':'id','type':'UUID','nullable':False},
                    {'name':'payload','type':'TEXT','nullable':True}], 'primary_key':['id']},
                {'kind':'add_column','table':'runs','name':'online_note','type':'TEXT','nullable':True},
                {'kind':'create_index','name':'new_jobs_payload','table':'new_jobs','columns':['payload'],'unique':False}]}
    value = {'from_revision':BASE,'to_revision':BASE+1,'compatibility':COMPATIBILITY,'steps':[step]}
    return seal(value)


def seal(value):
    value['digest'] = online.digest({k:v for k,v in value.items() if k != 'digest'})
    return value


@pytest.fixture
def database():
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set MOYAI_TEST_POSTGRES_URL to disposable PostgreSQL.')
    schema = 'moyai_online_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE TABLE "{schema}".schema_state(id INTEGER PRIMARY KEY,revision INTEGER NOT NULL,dirty INTEGER NOT NULL)')
        conn.execute(f'INSERT INTO "{schema}".schema_state VALUES(1,%s,0)', (BASE,))
        conn.execute(f'CREATE TABLE "{schema}".runs(id TEXT PRIMARY KEY,counter BIGINT NOT NULL)')
        conn.execute(f'INSERT INTO "{schema}".runs VALUES(\'saved\',0)')
    yield url, schema
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def apply(database, value=None, **kwargs):
    url, schema = database
    with psycopg.connect(url, autocommit=True) as conn:
        return online.apply_plan(conn, schema, value or expansion(), **kwargs)


def rows(database, query, params=()):
    url, schema = database
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'SET search_path TO "{schema}",pg_catalog')
        return conn.execute(query, params).fetchall()


def assert_untouched(database):
    assert rows(database, 'SELECT revision,dirty FROM schema_state') == [(BASE,0)]
    assert rows(database, 'SELECT tablename FROM pg_tables WHERE schemaname=current_schema() ORDER BY tablename') == [('runs',),('schema_state',)]
    assert rows(database, 'SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=\'runs\' ORDER BY column_name') == [('counter',),('id',)]


def test_schema_contract_uses_compatibility_floor_not_physical_revision(monkeypatch):
    assert online.schema_support() == {'protocol':1,'required':BASE,'compatibility':COMPATIBILITY}
    value = online.plan(BASE)
    assert value == seal({'from_revision':BASE,'to_revision':BASE,'compatibility':COMPATIBILITY,'steps':[]})
    settings = Settings(_env_file=None)
    wanted = {'protocol':runtime_compatibility.API_PROTOCOL_REVISION,'schema':COMPATIBILITY,
              'settings':{k:getattr(settings,k) for k in RUNTIME_SETTINGS if k!='moyai_build_sha'}}
    old = hashlib.sha256(json.dumps(wanted,sort_keys=True).encode()).hexdigest()
    assert runtime_compatibility.api_fingerprint(settings) == old
    before = {role:runtime_compatibility.background_fingerprint(settings,role) for role in ('worker','broker','coordinator')}
    monkeypatch.setattr(schema_migrations,'SCHEMA_REVISION',BASE+1)
    assert runtime_compatibility.api_fingerprint(settings) == old
    assert {role:runtime_compatibility.background_fingerprint(settings,role) for role in before} == before


def test_candidate_plans_require_contiguous_reviewed_source_digests(monkeypatch):
    value = expansion()
    monkeypatch.setattr(schema_migrations,'SCHEMA_REVISION',BASE+1)
    versions = dict(online._versions(), **{str(BASE+1):'a'*64})
    monkeypatch.setattr(online,'_versions',lambda:versions)
    monkeypatch.setattr(online,'_manifest',lambda:{'protocol':1,'steps':value['steps']})
    assert online.plan(BASE) == value
    value['steps'][0]['target_digest'] = 'b'*64
    with pytest.raises(DatabaseError,match='versioned schema source'):
        online.plan(BASE)
    monkeypatch.setattr(online,'_manifest',lambda:{'protocol':1,'steps':[]})
    with pytest.raises(DatabaseError,match='No reviewed'):
        online.plan(BASE)


def test_performance_expansion_preserves_old_writer_and_matches_initializer(database, tmp_path):
    """Exercise the real 20 -> 21 declaration, not only a synthetic future plan."""
    from app import performance
    url, schema = database
    columns = {'message_id': 'BIGINT PRIMARY KEY', 'run_id': 'TEXT NOT NULL',
               'kind': 'TEXT NOT NULL', 'received_at': 'TEXT', 'persisted_at': 'TEXT NOT NULL',
               'send_now_at': 'TEXT', 'send_now_kind': 'TEXT', 'picked_up_at': 'TEXT',
               'pickup_kind': 'TEXT', 'first_model_at': 'TEXT'}
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'SET search_path TO "{schema}",pg_catalog')
        conn.execute('UPDATE schema_state SET revision=20')
        conn.execute('CREATE TABLE performance_samples (' +
                     ','.join(name + ' ' + kind for name, kind in columns.items()) + ')')
        conn.execute("INSERT INTO performance_samples(message_id,run_id,kind,persisted_at,first_model_at) "
                     "VALUES(1,'saved','new_chat','2026-10-15T00:00:00+00:00','2026-10-15T00:00:01+00:00')")
        # Same connection remains a live old writer across the additive DDL.
        plan = online.plan(20)
        assert online.apply_plan(conn, schema, plan)['status'] == 'applied'
        assert online.schema_is_compatible(conn, schema, 20, 20)
        assert online.schema_is_compatible(conn, schema, 21, 20)
        conn.execute("UPDATE performance_samples SET first_model_at='2026-10-15T00:00:02+00:00' WHERE message_id=1")
        assert conn.execute('SELECT timing_version,dispatch_at FROM performance_samples').fetchone() == (None, None)
        actual = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='performance_samples'",
            (schema,))}
    sqlite = Store(tmp_path / 'initializer')
    try:
        with sqlite.connect() as conn:
            assert actual == set(conn.column_names('performance_samples'))
    finally:
        sqlite.close()
    new = Store(tmp_path / 'new-reader', database_url=url, database_schema=schema, schema_mode='verify')
    try:
        performance.model_dispatched(new, {'id': 'saved', 'active_message_id': 1},
                                     '2026-10-15T00:00:01+00:00', '2026-10-15T00:00:03+00:00', 'request')
        assert new.rows('SELECT first_model_at,dispatch_at,timing_version FROM performance_samples') == [
            {'first_model_at': '2026-10-15T00:00:02+00:00', 'dispatch_at': '2026-10-15T00:00:03+00:00',
             'timing_version': 2}]
    finally:
        new.close()


@pytest.mark.parametrize('mutation', ['drop','default','not_null','index_existing','sql_type',
                                     'foreign_key','gap','breaking','digest','metadata'])
def test_unsafe_or_drifting_plan_rejected_before_database_access(mutation):
    value = expansion()
    ops = value['steps'][0]['operations']
    if mutation == 'drop': ops[0]['kind']='drop_table'
    elif mutation == 'default': ops[1]['default']='clock_timestamp()'
    elif mutation == 'not_null': ops[1]['nullable']=False
    elif mutation == 'index_existing': ops[2]['table']='runs'
    elif mutation == 'sql_type': ops[1]['type']='TEXT; DROP TABLE runs'
    elif mutation == 'foreign_key': ops[0]['foreign_keys']=[]
    elif mutation == 'gap': value['steps'][0]['from_revision']+=1
    elif mutation == 'breaking': value['steps'][0]['compatibility']+=1
    elif mutation == 'metadata': ops[1]['table']='schema_state'
    if mutation != 'digest': seal(value)
    else: value['digest']='f'*64
    with pytest.raises(DatabaseError):
        online.validate_plan(value)


def test_live_old_writer_and_old_new_constructors_survive_expansion(database,tmp_path,monkeypatch):
    url, schema = database
    old = Store(tmp_path/'old',database_url=url,database_schema=schema,schema_mode='verify')
    locked, release = threading.Event(), threading.Event()
    with psycopg.connect(url,autocommit=True) as old_owner:
        old_owner.execute('SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)',(schema,))
        def before():
            locked.set()
            assert release.wait(5)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(apply,database,before_apply=before)
                assert locked.wait(5)
                # A migration holding its locks does not take the global writer lock.
                old.execute('UPDATE runs SET counter=counter+1 WHERE id=?',('saved',))
                assert old.rows('SELECT counter FROM runs') == [{'counter':1}]
                release.set()
                assert pending.result(timeout=10)['status']=='applied'
            old.execute('UPDATE runs SET counter=counter+1 WHERE id=?',('saved',))
            assert old.rows('SELECT counter,online_note FROM runs') == [{'counter':2,'online_note':None}]
            versions=dict(online._versions(),**{str(BASE+1):'a'*64})
            monkeypatch.setattr(online,'_versions',lambda:versions)
            for required in (BASE,BASE+1):
                monkeypatch.setattr(schema_migrations,'SCHEMA_REVISION',required)
                replacement=Store(tmp_path/str(required),database_url=url,database_schema=schema,schema_mode='verify')
                try:
                    assert replacement.rows('SELECT counter FROM runs') == [{'counter':2}]
                finally:
                    replacement.close()
        finally:
            release.set()
            old.close()
    assert rows(database,'SELECT count(*) FROM online_schema_migrations') == [(1,)]


def test_contention_aborts_quickly_and_rolls_back_all_ddl_receipts_and_revision(database):
    url, schema = database
    with psycopg.connect(url) as blocker:
        blocker.execute(f'SELECT * FROM "{schema}".runs')
        started=time.monotonic()
        with pytest.raises(DatabaseError,match='result is uncertain'):
            apply(database)
        assert time.monotonic()-started < 2
    assert_untouched(database)


def test_offline_owner_prevents_online_ddl_without_waiting(database):
    url, schema=database
    with psycopg.connect(url,autocommit=True) as owner:
        owner.execute('SELECT pg_advisory_lock(726940,%s::regnamespace::oid::int)',(schema,))
        started=time.monotonic()
        with pytest.raises(DatabaseError,match='Offline migration owns'):
            apply(database)
        assert time.monotonic()-started < 1
    assert_untouched(database)


def test_two_migrators_and_committed_retry_are_not_duplicate_ddl(database):
    locked,release=threading.Event(),threading.Event()
    def hold():
        locked.set()
        assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(apply,database,before_apply=hold)
        try:
            assert locked.wait(5)
            with pytest.raises(DatabaseError,match='Another online migration'):
                apply(database)
        finally:
            release.set()
        assert first.result(timeout=10)['status']=='applied'
    assert apply(database)['status']=='already_applied'
    assert rows(database,'SELECT count(*) FROM online_schema_migrations') == [(1,)]
    changed=expansion()
    changed['steps'][0]['operations'][1]['name']='different_column'
    seal(changed)
    with pytest.raises(DatabaseError,match='differs from this plan'):
        apply(database,changed)


def test_callback_runs_with_both_locks_before_any_ddl_and_failure_rolls_back(database):
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        def reject():
            locks={row[0] for row in conn.execute("SELECT classid FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted")}
            assert {726940,online.MIGRATION_LOCK} <= locks
            assert conn.execute('SELECT to_regclass(%s)',(f'{schema}.new_jobs',)).fetchone()==(None,)
            raise DatabaseError('Release owner guard rejected migration.')
        with pytest.raises(DatabaseError,match='Release owner guard'):
            online.apply_plan(conn,schema,expansion(),before_apply=reject)
    assert_untouched(database)


def test_unknown_newer_revision_and_tampered_receipt_fail_old_constructor(database,tmp_path):
    url,schema=database
    rows(database,'UPDATE schema_state SET revision=%s RETURNING revision',(BASE+1,))
    with pytest.raises(DatabaseError,match='incomplete, or incompatible'):
        PostgresDatabase(url,schema,schema_mode='verify')
    rows(database,'UPDATE schema_state SET revision=%s RETURNING revision',(BASE,))
    apply(database)
    rows(database,"UPDATE online_schema_migrations SET step_digest='changed' RETURNING from_revision")
    with pytest.raises(DatabaseError,match='incomplete, or incompatible'):
        PostgresDatabase(url,schema,schema_mode='verify')
    with pytest.raises(DatabaseError,match='no verified schema digest'):
        apply(database)


def test_plan_source_digest_drift_and_dirty_state_reject_without_ddl(database):
    value=expansion()
    value['steps'][0]['source_digest']='e'*64
    seal(value)
    with pytest.raises(DatabaseError,match='locally known schema digest'):
        apply(database,value)
    assert_untouched(database)
    rows(database,'UPDATE schema_state SET dirty=1 RETURNING dirty')
    with pytest.raises(DatabaseError,match='exact clean source'):
        apply(database)
    assert rows(database,'SELECT to_regclass(current_schema() || \'.online_schema_migrations\')') == [(None,)]


def test_noop_never_creates_ledger_and_active_transaction_is_rejected(database):
    assert apply(database,online.plan(BASE)) == {'status':'already_applied','from_revision':BASE,
                                               'to_revision':BASE,'digest':online.plan(BASE)['digest']}
    assert_untouched(database)
    url,schema=database
    with psycopg.connect(url) as conn:
        conn.execute('SELECT 1')
        with pytest.raises(DatabaseError,match='idle dedicated connection'):
            online.apply_plan(conn,schema,expansion())
    assert_untouched(database)


def test_overall_budget_rolls_back_before_next_operation(database,monkeypatch):
    original=online._ddl
    def delayed(conn,schema,op):
        original(conn,schema,op)
        monkeypatch.setattr(online.time,'monotonic',lambda:10**12)
    monkeypatch.setattr(online,'_ddl',delayed)
    with pytest.raises(DatabaseError,match='transaction budget'):
        apply(database)
    assert_untouched(database)


def test_offline_migrator_semantics_still_reject_downgrade(database):
    apply(database)
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        with pytest.raises(DatabaseError,match='downgrades are not supported'):
            schema_migrations.prepare_schema(conn,schema,'migrate')
    assert rows(database,'SELECT revision,dirty FROM schema_state') == [(BASE+1,0)]


def test_installed_bridge_accepts_later_step_only_through_verified_prior_chain(database):
    first=expansion()
    apply(database,first)
    second=deepcopy(first)
    second.update(from_revision=BASE+1,to_revision=BASE+2)
    step=second['steps'][0]
    step.update(from_revision=BASE+1,to_revision=BASE+2,source_digest='a'*64,target_digest='b'*64,
                operations=[{'kind':'add_column','table':'runs','name':'second_note','type':'TEXT','nullable':True}])
    seal(second)
    assert apply(database,second)['status']=='applied'
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        assert online.schema_is_compatible(conn,schema,BASE,COMPATIBILITY)
    # Losing an intermediate receipt cannot be hidden by the final receipt.
    rows(database,'DELETE FROM online_schema_migrations WHERE from_revision=%s RETURNING from_revision',(BASE,))
    with psycopg.connect(url,autocommit=True) as conn:
        assert not online.schema_is_compatible(conn,schema,BASE,COMPATIBILITY)


def test_lost_commit_acknowledgement_reports_uncertainty_and_receipt_allows_inspected_retry(database):
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        class LostAcknowledgement:
            info=conn.info
            execute=conn.execute

            @contextmanager
            def transaction(self):
                with conn.transaction():
                    yield
                raise psycopg.OperationalError('private connection details after successful COMMIT')

        with pytest.raises(DatabaseError,match='result is uncertain') as error:
            online.apply_plan(LostAcknowledgement(),schema,expansion())
        assert 'private connection' not in str(error.value)
        assert 'rolled back' not in str(error.value)
    assert rows(database,'SELECT revision,dirty FROM schema_state') == [(BASE+1,0)]
    assert rows(database,'SELECT count(*) FROM online_schema_migrations') == [(1,)]
    assert apply(database)['status']=='already_applied'


def test_newer_binary_does_not_accept_changed_committed_receipt(database,monkeypatch):
    apply(database)
    monkeypatch.setattr(schema_migrations,'SCHEMA_REVISION',BASE+1)
    versions=dict(online._versions(),**{str(BASE+1):'a'*64})
    monkeypatch.setattr(online,'_versions',lambda:versions)
    rows(database,"UPDATE online_schema_migrations SET compatibility=compatibility+1 RETURNING from_revision")
    with pytest.raises(DatabaseError,match='no verified schema digest'):
        apply(database)


def test_exact_new_binary_rejects_conflicting_online_target_digest(database,monkeypatch):
    apply(database)
    versions=dict(online._versions(),**{str(BASE+1):'b'*64})
    monkeypatch.setattr(online,'_versions',lambda:versions)
    monkeypatch.setattr(schema_migrations,'SCHEMA_REVISION',BASE+1)
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        assert not online.schema_is_compatible(conn,schema,BASE+1,COMPATIBILITY)
        with pytest.raises(DatabaseError,match='incomplete, or incompatible'):
            schema_migrations.prepare_schema(conn,schema,'verify')
    # An explicitly offline-created exact revision retains its prior semantics.
    with psycopg.connect(url,autocommit=True) as conn:
        conn.execute(f'DROP TABLE "{schema}".online_schema_migrations')
        assert online.schema_is_compatible(conn,schema,BASE+1,COMPATIBILITY)


def test_malformed_incoming_receipt_is_incompatible(database,monkeypatch):
    apply(database)
    versions=dict(online._versions(),**{str(BASE+1):'a'*64})
    monkeypatch.setattr(online,'_versions',lambda:versions)
    rows(database,"UPDATE online_schema_migrations SET step='[]'::jsonb RETURNING from_revision")
    url,schema=database
    with psycopg.connect(url,autocommit=True) as conn:
        assert not online.schema_is_compatible(conn,schema,BASE+1,COMPATIBILITY)


def adopt_expanded_baseline(monkeypatch, *, required=BASE+1):
    versions = dict(online._versions(), **{str(BASE+1): 'a'*64, str(BASE+2): 'b'*64})
    monkeypatch.setattr(online, '_versions', lambda: versions)
    monkeypatch.setattr(schema_migrations, 'SCHEMA_REVISION', required)
    monkeypatch.setattr(schema_migrations, 'SCHEMA_COMPATIBILITY_REVISION', BASE+1)
    monkeypatch.setattr(online, '_manifest', lambda: {'protocol': 1, 'steps': []})


def expansion_after_baseline():
    value = expansion()
    value.update(from_revision=BASE+1, to_revision=BASE+2, compatibility=BASE+1)
    value['steps'][0].update(from_revision=BASE+1, to_revision=BASE+2, compatibility=BASE+1,
        source_digest='a'*64, target_digest='b'*64,
        operations=[{'kind':'add_column','table':'runs','name':'after_baseline','type':'TEXT','nullable':True}])
    return seal(value)


def test_exact_schema_at_new_compatibility_floor_preserves_historical_receipt(database, monkeypatch):
    apply(database)
    before = rows(database, 'SELECT * FROM online_schema_migrations')
    adopt_expanded_baseline(monkeypatch)
    url, schema = database
    db = PostgresDatabase(url, schema, schema_mode='verify')
    db.close()
    assert apply(database, online.plan(BASE+1))['status'] == 'already_applied'
    assert rows(database, 'SELECT * FROM online_schema_migrations') == before


def test_expansion_after_compatibility_floor_enforces_new_forward_contract(database, monkeypatch):
    apply(database)
    adopt_expanded_baseline(monkeypatch)
    assert apply(database, expansion_after_baseline())['status'] == 'applied'
    url, schema = database
    db = PostgresDatabase(url, schema, schema_mode='verify')
    db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        assert not online.schema_is_compatible(conn, schema, BASE, COMPATIBILITY)
    assert apply(database, expansion_after_baseline())['status'] == 'already_applied'


@pytest.mark.parametrize('expanded', [False, True])
@pytest.mark.parametrize('mutation', ['source', 'target', 'step_digest', 'compatibility', 'malformed'])
def test_historical_receipt_corruption_rejected_at_and_above_new_floor(database, monkeypatch, expanded, mutation):
    from psycopg.types.json import Jsonb
    first = expansion()
    apply(database, first)
    adopt_expanded_baseline(monkeypatch)
    if expanded:
        apply(database, expansion_after_baseline())
    if mutation in {'source', 'target'}:
        # Self-consistent receipt metadata still must match the binary's known schema.
        step = deepcopy(first['steps'][0])
        step[mutation + '_digest'] = 'f'*64
        rows(database, f'UPDATE online_schema_migrations SET {mutation}_digest=%s,step=%s,step_digest=%s '
             'WHERE from_revision=%s RETURNING from_revision',
             ('f'*64, Jsonb(step), online.digest(step), BASE))
    else:
        change = {'step_digest': "step_digest='changed'", 'compatibility': 'compatibility=compatibility+1',
                  'malformed': "step='[]'::jsonb"}[mutation]
        rows(database, 'UPDATE online_schema_migrations SET ' + change +
             ' WHERE from_revision=%s RETURNING from_revision', (BASE,))
    url, schema = database
    with pytest.raises(DatabaseError, match='incomplete, or incompatible'):
        PostgresDatabase(url, schema, schema_mode='verify')
    value = expansion_after_baseline() if expanded else online.plan(BASE+1)
    with pytest.raises(DatabaseError, match='no verified schema digest'):
        apply(database, value)


@pytest.mark.parametrize('corrupt', [False, True])
def test_newer_migrator_validates_receipt_for_known_earlier_source(database, monkeypatch, corrupt):
    apply(database)
    adopt_expanded_baseline(monkeypatch, required=BASE+2)
    if corrupt:
        rows(database, "UPDATE online_schema_migrations SET step_digest='changed' RETURNING from_revision")
        with pytest.raises(DatabaseError, match='no verified schema digest'):
            apply(database, expansion_after_baseline())
        assert rows(database, 'SELECT revision,dirty FROM schema_state') == [(BASE+1, 0)]
        assert rows(database, 'SELECT count(*) FROM online_schema_migrations') == [(1,)]
    else:
        assert apply(database, expansion_after_baseline())['status'] == 'applied'
