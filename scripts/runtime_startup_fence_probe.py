"""Exercise startup admission against disposable PostgreSQL; no provider calls.

Constructs the actual application components without entering their background
lifecycles. The model request is a synthetic pending accounting row, not a stream.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg

from app.config import Settings
from app.database import DatabaseError, runtime_fingerprint
from app.main import create_app


def probe(url, output, hold_seconds=0):
    started, events, report = time.monotonic(), [], {'passed': False}

    def record(message):
        print(message, flush=True)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])
        if hold_seconds:
            time.sleep(hold_seconds)

    schema = 'moyai_startup_probe_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-startup-probe-') as directory, ExitStack() as cleanup:
            settings = Settings(_env_file=None, data_dir=Path(directory),
                moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
                moyai_runtime_role='coordinator', moyai_separate_broker=True, moyai_build_sha='a' * 40,
                temporal_enabled=True, temporal_tls=False, object_storage_bucket='synthetic-startup-probe',
                session_secret='synthetic-session-key', encryption_key=Fernet.generate_key().decode(),
                session_titles_enabled=False, memory_review_enabled=False, litellm_spend_recovery_enabled=False)

            def start(role, build='a' * 40):
                app = create_app(settings.model_copy(update={'moyai_runtime_role': role, 'moyai_build_sha': build}))
                cleanup.callback(app.state.store.close)
                return app

            def reject(role, expected, build='a' * 40):
                try:
                    start(role, build)
                except DatabaseError as exc:
                    assert expected in str(exc), str(exc)
                else:
                    raise AssertionError(f'{role} unexpectedly started')

            record('START  Disposable PostgreSQL; actual app startup; no provider calls.')
            api, broker, worker = start('coordinator'), start('broker'), start('worker')
            store = broker.state.store
            run = store.create_run('Synthetic startup-fence check', '', 'modal', [])
            request = broker.state.spend.begin(run, 'synthetic-model')
            store.execute("UPDATE runs SET model='' WHERE id=?", (run['id'],))

            def snapshot():
                return {
                    'functions': store.rows('''SELECT proname,xmin::text AS version FROM pg_proc
                        WHERE pronamespace=?::regnamespace AND proname IN ('unicode_lower','json_text','json_number')
                        ORDER BY proname''', (schema,)),
                    'model': store.rows('SELECT model FROM runs WHERE id=?', (run['id'],))[0]['model'],
                    'policy': store.database.stored_policy(),
                    'request': store.rows('SELECT status FROM model_requests WHERE id=?', (request,))[0]['status'],
                }

            before = snapshot()
            record('READY  Coordinator, worker and broker own the same build; receipt is pending.')
            record('TRY    Start a worker from a different build.')
            reject('worker', 'same shared runtime configuration', 'b' * 40)
            assert snapshot() == before
            record('PASS   Worker rejected before schema writes or legacy model backfill.')
            reject('broker', 'Another inference broker')
            assert snapshot() == before
            record('PASS   Duplicate broker rejected before schema writes.')
            api.state.store.close()
            record('TRY    Replace the API with a different build while worker/broker remain.')
            reject('coordinator', 'Drain and stop', 'b' * 40)
            assert snapshot() == before
            record('PASS   New build rejected; policy, schema and pending receipt unchanged.')
            replacement = start('coordinator')
            assert store.rows('SELECT status FROM model_requests WHERE id=?', (request,))[0]['status'] == 'pending'
            record('PASS   Same-build API restart succeeds; broker still owns the pending receipt.')
            replacement.state.store.close()
            worker.state.store.close()
            store.close()
            record('STOP   All old runtime owners have exited; perform the drained upgrade.')
            upgraded = start('coordinator', 'b' * 40)
            start('broker', 'b' * 40)
            start('worker', 'b' * 40)
            assert upgraded.state.store.database.stored_policy() == runtime_fingerprint(
                settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
            record('PASS   Drained upgrade succeeds; matching broker and worker can join.')
            report.update(passed=True, real_postgres=True, actual_app_constructors=True,
                incompatible_worker_rejected_before_writes=True, duplicate_broker_rejected_before_writes=True,
                changed_coordinator_rejected_with_live_owners=True, pending_receipt_preserved=True,
                same_build_restart=True, drained_upgrade=True, provider_calls=0,
                background_lifecycles_started=False, production_changed=False)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        output.mkdir(parents=True, exist_ok=True)
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        header = {'version': 2, 'width': 110, 'height': 20, 'timestamp': int(time.time()),
                  'title': 'Moyai: startup admission before schema writes'}
        (output / 'startup-fences.cast').write_text('\n'.join(json.dumps(e) for e in [header, *events]) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--hold-seconds', type=float, default=0, help='Pause after each recorded result for readability.')
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local PostgreSQL database.')
    if not 0 <= args.hold_seconds <= 2:
        parser.error('Use --hold-seconds between 0 and 2.')
    probe(url, args.output, args.hold_seconds)


if __name__ == '__main__':
    main()
