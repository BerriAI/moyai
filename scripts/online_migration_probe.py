"""Real local PostgreSQL/process rehearsal of a synthetic future schema expansion.

Uses only MOYAI_TEST_POSTGRES_URL and a disposable unique schema. No Render,
Temporal, model provider, or application background lifecycle is started.
The installed bridge writer keeps its actual source revision throughout.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql

from app.config import Settings
from app.database import DatabaseError, PostgresDatabase
from app.db import Store
from app import online_migrations as online
from app.schema_migrations import migrate


def wait_for(predicate, message, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(.02)
    raise AssertionError(message)


def expansion(revision, source_digest, compatibility, name):
    step = {'from_revision': revision, 'to_revision': revision + 1,
            'compatibility': compatibility, 'source_digest': source_digest,
            'target_digest': online.digest({'synthetic_future_revision': revision + 1, 'column': name}),
            'operations': [{'kind': 'add_column', 'table': 'runs', 'name': name,
                            'type': 'TEXT', 'nullable': True}]}
    value = {'from_revision': revision, 'to_revision': revision + 1,
             'compatibility': compatibility, 'steps': [step]}
    value['digest'] = online.digest(value)
    return online.validate_plan(value)


def child(mode, directory):
    settings = Settings(_env_file=None, **json.loads(os.environ['MOYAI_PROBE_SETTINGS']))
    run_id = os.environ['MOYAI_PROBE_RUN']
    if mode == 'interrupted':
        # The first real DDL has executed but remains uncommitted. Let the
        # parent SIGKILL this process, as a lost migration host would do.
        original = online._ddl
        def held_ddl(conn, schema, operation):
            original(conn, schema, operation)
            (directory / 'ddl-entered').write_text('uncommitted\n')
            signal.alarm(10)
            signal.pause()
        online._ddl = held_ddl
        with psycopg.connect(settings.moyai_database_url, autocommit=True) as conn:
            online.apply_plan(conn, settings.moyai_database_schema,
                              json.loads(os.environ['MOYAI_PROBE_PLAN']))
        return 1

    db = PostgresDatabase(settings.moyai_database_url, settings.moyai_database_schema,
        application_instance=True, runtime_role='worker', runtime_settings=settings,
        schema_mode='verify', pool_size=2)
    try:
        db.configure_runtime(settings)
        if mode == 'reader':
            with db.connect() as conn:
                row = conn.execute('SELECT id,model_calls FROM runs WHERE id=?', (run_id,)).fetchone()
            assert row and row['id'] == run_id and row['model_calls'] > 0
            print(json.dumps({'passed': True, 'required_schema': online.schema_support()['required'],
                              'owner_pid': db.owner_pid}), flush=True)
            return 0

        from app.runtime_coordination import Lease, held_leases
        name, token = 'session:' + run_id, uuid4().hex
        assert db.acquire_lease(name, token, 30)
        lease_context = held_leases.set((Lease(settings.moyai_database_schema, name, token, 0),))
        result = {'passed': False, 'writes': 0, 'max_write_ms': 0, 'owner_pid': db.owner_pid,
                  'required_schema': online.schema_support()['required']}
        (directory / 'writer-ready.json').write_text(json.dumps(result))
        deadline = time.monotonic() + 45
        try:
            while time.monotonic() < deadline and not (directory / 'stop-writer').exists():
                began = time.monotonic()
                assert db.renew_lease(name, token, 30)
                with db.connect(write_scope=run_id) as conn:
                    conn.execute('UPDATE runs SET model_calls=model_calls+1 WHERE id=?', (run_id,))
                result['writes'] += 1
                result['max_write_ms'] = max(result['max_write_ms'], round((time.monotonic()-began)*1000, 2))
                time.sleep(.01)
            result['passed'] = (directory / 'stop-writer').exists()
        finally:
            held_leases.reset(lease_context)
            db.release_lease(name, token)
            (directory / 'writer-result.json').write_text(json.dumps(result))
        return 0 if result['passed'] else 1
    finally:
        db.close()


def probe(url, output):
    began, events, processes = time.monotonic(), [], []
    report = {'passed': False, 'scope': 'local_postgres_synthetic_future_schema',
              'production_deployment_proof': False, 'provider_calls': 0}
    schema = 'moyai_online_probe_' + uuid4().hex
    support = online.schema_support()

    def record(message):
        print(message, flush=True)
        events.append([round(time.monotonic()-began, 3), 'o', message + '\r\n'])

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    coordinator = None
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-online-probe-') as temporary:
            directory = Path(temporary)
            settings = Settings(_env_file=None, data_dir=directory,
                moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
                moyai_schema_mode='verify', moyai_runtime_role='coordinator',
                moyai_coordinator_handoff=True, moyai_separate_broker=True,
                moyai_build_sha='a' * 40, temporal_enabled=True, temporal_tls=False,
                object_storage_bucket='synthetic-online-probe', session_secret='synthetic-online-key',
                encryption_key=Fernet.generate_key().decode())
            migrate(settings)
            coordinator = Store(directory, database_url=url, database_schema=schema,
                application_instance=True, runtime_role='coordinator', runtime_settings=settings,
                schema_mode='verify', database_pool_size=2)
            coordinator.database.configure_runtime(settings)
            run = coordinator.create_run('Synthetic online migration fixture', '', 'modal', [])
            worker = settings.model_copy(update={'moyai_runtime_role': 'worker', 'moyai_database_initialize': False})
            env = {**os.environ, 'MOYAI_PROBE_SETTINGS': worker.model_dump_json(), 'MOYAI_PROBE_RUN': run['id']}

            def start(mode, extra=None):
                process = subprocess.Popen([sys.executable, '-m', 'scripts.online_migration_probe',
                    '--child', mode, '--directory', str(directory)],
                    env={**env, **(extra or {})}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                return process

            def query(statement, params=()):
                with psycopg.connect(url) as conn:
                    conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
                    return conn.execute(statement, params).fetchall()

            def count():
                return query('SELECT model_calls FROM runs WHERE id=%s', (run['id'],))[0][0]

            def policies():
                return {role: query('SELECT * FROM runtime_' + role + '_policy')
                        for role in ('api', 'worker', 'broker', 'coordinator')} | {'cluster': query('SELECT * FROM runtime_policy')}

            record(f"START  Real PostgreSQL; installed bridge schema {support['required']}; no provider calls.")
            active = start('writer')
            wait_for(lambda: (directory / 'writer-ready.json').exists(), 'Old writer did not become ready')
            wait_for(lambda: count() >= 5, 'Old writer did not commit initial work')
            before_count, before_policy = count(), policies()
            owner = json.loads((directory / 'writer-ready.json').read_text())['owner_pid']
            record('READY  Separate old worker process is committing scoped writes and renewing its execution lease.')

            value = expansion(support['required'], online._versions()[str(support['required'])],
                              support['compatibility'], 'online_probe_added')
            with psycopg.connect(url, autocommit=True) as conn:
                applied = online.apply_plan(conn, schema, value)
                repeated = online.apply_plan(conn, schema, value)
            assert applied['status'] == 'applied' and repeated['status'] == 'already_applied'
            assert applied['digest'] == repeated['digest'] == value['digest']
            assert query('SELECT revision,dirty FROM schema_state') == [(value['to_revision'], 0)]
            assert query('SELECT online_probe_added FROM runs WHERE id=%s', (run['id'],)) == [(None,)]
            assert query('SELECT count(*) FROM online_schema_migrations') == [(1,)]
            wait_for(lambda: count() >= before_count + 5, 'Old worker stopped writing after expansion')
            assert active.poll() is None and policies() == before_policy
            assert query('SELECT count(*) FROM runtime_leases WHERE name=%s AND expires_at>clock_timestamp()',
                         ('session:' + run['id'],)) == [(1,)]
            record('PASS   Compatible column committed atomically; same old worker and lease continue; exact retry changes nothing.')

            rollback_reader = start('reader')
            stdout, _ = rollback_reader.communicate(timeout=15)
            assert rollback_reader.returncode == 0
            reader = json.loads(stdout)
            assert reader['passed'] and reader['required_schema'] == support['required']
            record('PASS   Fresh old-source process reads existing rows; expanded-column reader sees its nullable default; policy preserved.')

            interrupted = expansion(value['to_revision'], value['steps'][-1]['target_digest'],
                                    support['compatibility'], 'online_probe_interrupted')
            doomed = start('interrupted', {'MOYAI_PROBE_PLAN': json.dumps(interrupted)})
            wait_for(lambda: (directory / 'ddl-entered').exists(), 'Migration never reached uncommitted DDL')
            doomed.kill()
            doomed.communicate(timeout=10)
            assert doomed.returncode == -signal.SIGKILL
            wait_for(lambda: query('SELECT count(*) FROM pg_stat_activity WHERE pid=%s', (owner,))[0][0],
                     'Old worker ownership disappeared')
            assert query('SELECT revision,dirty FROM schema_state') == [(value['to_revision'], 0)]
            assert query('''SELECT count(*) FROM information_schema.columns
                WHERE table_schema=%s AND table_name='runs' AND column_name='online_probe_interrupted' ''', (schema,)) == [(0,)]
            assert query('SELECT count(*) FROM online_schema_migrations') == [(1,)]
            resumed_count = count()
            wait_for(lambda: count() > resumed_count, 'Old worker did not resume after migration host loss')
            with psycopg.connect(url, autocommit=True) as conn:
                assert online.apply_plan(conn, schema, interrupted)['status'] == 'applied'
            record('PASS   SIGKILL after real DDL leaves no partial column, schema bump, or receipt; inspected retry succeeds.')

            rejected = expansion(interrupted['to_revision'], interrupted['steps'][-1]['target_digest'],
                                 support['compatibility'], 'online_probe_forbidden')
            breaking = deepcopy(rejected)
            breaking['steps'][0]['operations'] = [{'kind': 'drop_column', 'table': 'runs', 'name': 'prompt'}]
            gap = deepcopy(rejected)
            gap['to_revision'] += 1
            for invalid in (breaking, gap):
                invalid['digest'] = online.digest({k: v for k, v in invalid.items() if k != 'digest'})
                reached_callback = []
                with psycopg.connect(url, autocommit=True) as conn:
                    try:
                        online.apply_plan(conn, schema, invalid, before_apply=lambda: reached_callback.append(True))
                    except DatabaseError:
                        pass
                    else:
                        raise AssertionError('Unsafe plan was accepted')
                assert not reached_callback
            assert query('SELECT revision,dirty FROM schema_state') == [(interrupted['to_revision'], 0)]
            assert query('SELECT count(*) FROM online_schema_migrations') == [(2,)]
            assert query('SELECT prompt FROM runs WHERE id=%s', (run['id'],)) == [('Synthetic online migration fixture',)]
            assert policies() == before_policy
            record('PASS   Breaking operation and incomplete path rejected before DDL; data, policies and committed receipts preserved.')

            (directory / 'stop-writer').write_text('stop\n')
            active.communicate(timeout=10)
            assert active.returncode == 0
            writer = json.loads((directory / 'writer-result.json').read_text())
            assert writer['passed'] and writer['owner_pid'] == owner and writer['writes'] == count()
            report.update(passed=True, real_postgres=True, separate_writer_process=True,
                source_schema=support['required'], compatibility=support['compatibility'],
                expanded_schema=value['to_revision'], old_source_reconnect=True,
                runtime_policy_unchanged=True, execution_lease_preserved=True,
                killed_migration_rolled_back=True, inspected_retry_succeeded=True,
                repeated_plan_status=repeated['status'], breaking_and_incomplete_rejected=True,
                writer_commits=writer['writes'], max_writer_transaction_ms=writer['max_write_ms'])
            record(f"DONE   {writer['writes']} committed writes; max observed write {writer['max_write_ms']} ms. Local rehearsal only.")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
        if coordinator is not None:
            coordinator.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        report['elapsed_seconds'] = round(time.monotonic()-began, 3)
        output.mkdir(parents=True, exist_ok=True)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        (output / 'README.txt').write_text('Real local PostgreSQL and separate runtime processes; synthetic future additive schemas.\n'
            'No Render deployment, real inference, Temporal lifecycle, or production continuity is claimed.\n'
            'report.json contains outcomes; online-migration.cast records the actual rehearsal output.\n')
        header = {'version': 2, 'width': 120, 'height': 22, 'timestamp': int(time.time()),
                  'title': 'Moyai: online expansion while an old worker continues writing'}
        (output / 'online-migration.cast').write_text('\n'.join(json.dumps(row) for row in [header, *events]) + '\n')
    print(json.dumps(report, sort_keys=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--child', choices=('writer', 'reader', 'interrupted'), help=argparse.SUPPRESS)
    parser.add_argument('--directory', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args.child, args.directory)
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url or args.output is None:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to disposable PostgreSQL and provide --output DIRECTORY.')
    return 0 if probe(url, args.output)['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Online migration rehearsal failed (' + type(exc).__name__ + ').', file=sys.stderr)
        raise SystemExit(1) from None
