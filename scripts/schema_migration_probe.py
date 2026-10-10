"""Record offline schema migration and real runtime-constructor checks on disposable PostgreSQL."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg

from app.config import Settings
from app.database import DatabaseError
from app.main import create_app


def probe(url, output, hold_seconds=0):
    started, events = time.monotonic(), []
    report = {'passed': False, 'production_changed': False, 'provider_calls': 0,
              'background_lifecycles_started': False}

    def record(message):
        print(message, flush=True)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])
        if hold_seconds:
            time.sleep(hold_seconds)

    schema = 'moyai_schema_probe_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-schema-probe-') as directory, ExitStack() as cleanup:
            settings = Settings(_env_file=None, data_dir=Path(directory),
                moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
                moyai_schema_mode='verify', moyai_runtime_role='coordinator', moyai_separate_broker=True,
                moyai_build_sha='a' * 40, temporal_enabled=True, temporal_tls=False,
                object_storage_bucket='synthetic-schema-probe', session_secret='synthetic-session-key',
                encryption_key=Fernet.generate_key().decode(), session_titles_enabled=False,
                memory_review_enabled=False, litellm_spend_recovery_enabled=False)
            env = {**os.environ, **{k.upper(): str(v) for k, v in settings.model_dump(mode='json').items()
                                   if isinstance(v, (str, int, float, bool))}}

            def migrate():
                return subprocess.run([sys.executable, '-m', 'app.schema_migrations', '--apply'],
                    env=env, capture_output=True, text=True, timeout=30)

            def start(role):
                app = create_app(settings.model_copy(update={'moyai_runtime_role': role}))
                cleanup.callback(app.state.store.close)
                return app

            record('START  Disposable PostgreSQL; real CLI and app constructors.')
            try:
                start('coordinator')
                raise AssertionError('Unprepared schema accepted')
            except DatabaseError:
                record('PASS   App refuses to start against an unprepared schema.')
            record('RUN    python -m app.schema_migrations --apply')
            applied = migrate()
            assert applied.returncode == 0, applied.stderr
            assert json.loads(applied.stdout)['status'] == 'ready'
            record('PASS   Schema revision 1 prepared; no worker or provider started.')
            api, broker, worker = start('coordinator'), start('broker'), start('worker')
            store = broker.state.store
            run = store.create_run('Synthetic pending receipt', '', 'modal', [])
            receipt = broker.state.spend.begin(run, 'synthetic-model')
            store.execute("UPDATE runs SET model='' WHERE id=?", (run['id'],))
            before = store.rows('''SELECT oid,xmin::text FROM pg_proc
                WHERE pronamespace=?::regnamespace ORDER BY oid''', (schema,))
            record('READY  App, broker and worker use verify-only schema startup.')
            rejected = migrate()
            assert rejected.returncode != 0 and 'Another Moyai instance' in rejected.stderr
            record('PASS   Migration refuses live owners; pending receipt unchanged.')
            api.state.store.close()
            replacement = start('coordinator')
            assert store.rows('''SELECT oid,xmin::text FROM pg_proc
                WHERE pronamespace=?::regnamespace ORDER BY oid''', (schema,)) == before
            assert store.run(run['id'])['model'] == ''
            assert store.rows('SELECT status FROM model_requests WHERE id=?', (receipt,))[0]['status'] == 'pending'
            record('PASS   App restart preserves schema, legacy row and broker receipt.')
            replacement.state.store.close()
            worker.state.store.close()
            store.close()
            # Simulate the durable state left by an interrupted migration.
            with psycopg.connect(url) as conn:
                conn.execute(f'UPDATE "{schema}".schema_state SET dirty=1')
            try:
                start('coordinator')
                raise AssertionError('Dirty schema accepted')
            except DatabaseError as exc:
                assert 'incomplete' in str(exc)
                record('PASS   An incomplete migration blocks app startup.')
            retry = migrate()
            assert retry.returncode == 0, retry.stderr
            start('coordinator')
            record('PASS   Offline retry completes; matching app startup succeeds.')
            report.update(passed=True, real_postgres=True, actual_cli=True,
                missing_schema_rejected=True, live_migration_rejected=True,
                schema_and_backfill_unchanged_on_restart=True, pending_receipt_preserved=True,
                dirty_schema_rejected=True, retry_succeeded=True)
            record('DONE   Schema prerequisite verified. Traffic overlap still follows.')
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        output.mkdir(parents=True, exist_ok=True)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        header = {'version': 2, 'width': 112, 'height': 20, 'timestamp': int(time.time()),
                  'title': 'Moyai: explicit migrations and startup without DDL'}
        (output / 'schema-migration.cast').write_text('\n'.join(json.dumps(e) for e in [header, *events]) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold-seconds', type=float, default=0)
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    if not 0 <= args.hold_seconds <= 2:
        parser.error('Use --hold-seconds between 0 and 2.')
    probe(url, args.output, args.hold_seconds)


if __name__ == '__main__':
    main()
