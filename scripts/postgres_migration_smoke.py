"""Run a real migration rehearsal with synthetic Moyai data and disposable Postgres.

Set MOYAI_TEST_POSTGRES_URL to a disposable database. Only this script's newly
created, randomly named schema is removed afterward. No cloud services are used.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from app.config import Settings
from app.main import create_app
from app import postgres_migration as migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON verification report; contains no row data.')
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL', '')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable database.')
    name = 'moyai_smoke_' + uuid4().hex
    created = False
    with tempfile.TemporaryDirectory(prefix='moyai-migration-demo-') as temporary:
        directory = Path(temporary)
        app = create_app(Settings(_env_file=None, data_dir=directory, session_titles_enabled=False))
        store = app.state.store
        try:
            for i in range(25):
                run = store.create_run(f'Synthetic session {i + 1} — 日本語 🗿', '', 'demo', [], chat_enabled=True)
                for step in range(3):
                    store.event(run['id'], 'status', f'Step {step + 1}', {'synthetic': True})
            store.attachments.save(uuid4().hex, 'demo', 'binary.bin', bytes(range(256)),
                                   ('application/octet-stream', b'', ''), 1024)
            ciphertext = app.state.security.fernet.encrypt(b'synthetic-token').decode()
            store.execute('INSERT INTO connections VALUES(?,?,?,?)', ('github', ciphertext, 'Demo only', '2026-10-09'))
            print('SQLite source: 25 synthetic chats, Unicode history, encrypted connection, binary attachment.', flush=True)
            before = migration.plan(directory)
            print(f"PLAN  {before['objects']['table']} tables / {before['total_rows']} rows validated. Source is read-only.", flush=True)
            copied = migration.transfer(directory, url, name)
            created = True
            assert copied['verified'] and copied['tables'] == before['tables']
            print(f"COPY  {copied['objects']['table']} tables imported into real PostgreSQL; every row hash matches.", flush=True)
            assert migration.transfer(directory, url, name, verify_only=True)['verified']
            print('VERIFY  Independent readback matches all table counts, types and contents.', flush=True)
            try:
                migration.transfer(directory, url, name)
            except migration.MaintenanceError:
                print('PROTECT  Reusing the destination was refused; existing data preserved.', flush=True)
            else:
                raise AssertionError('An existing destination was overwritten')
            assert migration.plan(directory)['tables'] == before['tables']
            print('SOURCE  Original SQLite rows and encrypted connection are unchanged.', flush=True)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(copied, indent=2) + '\n')
            print('PASS  Migration rehearsal complete. Application cutover is a later step.', flush=True)
        finally:
            store.objects.close()
            if created:
                with psycopg.connect(url, autocommit=True) as conn:
                    conn.execute(f'DROP SCHEMA {migration.identifier(name)} CASCADE')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('FAIL  Rehearsal did not complete. Check the disposable database and run the migration tests.', file=sys.stderr)
        raise SystemExit(1)
