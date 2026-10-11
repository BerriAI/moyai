"""Exercise Moyai's real HTTP API across two server processes, using Postgres.

MOYAI_TEST_POSTGRES_URL must point to a disposable local database. The script
creates and removes only its own random schema and uses synthetic demo chats.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]


def wait_for(function, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    raise RuntimeError('The local demo did not reach its expected state.')


@contextmanager
def server(url, schema, directory, *, initialize):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    environment = {
        'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'PYTHONUNBUFFERED': '1',
        'DATA_DIR': str(directory), 'PUBLIC_URL': origin, 'AGENT_HARNESS': 'hermes',
        'MOYAI_DATABASE_URL': url, 'MOYAI_DATABASE_SCHEMA': schema,
        'MOYAI_DATABASE_INITIALIZE': str(initialize).lower(),
        'TEMPORAL_ENABLED': 'false', 'SESSION_TITLES_ENABLED': 'false', 'DEMO_STEP_SECONDS': '0.01',
    }
    # Keep server diagnostics private: only validated results go to the recording.
    with tempfile.TemporaryFile() as logs:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                    '--port', str(port), '--log-level', 'error'], cwd=directory,
                                   env=environment, stdout=logs, stderr=logs)
        try:
            with httpx.Client(base_url=origin, timeout=10) as client:
                wait_for(lambda: client.get('/health').status_code == 200)
                session = client.get('/api/session')
                session.raise_for_status()
                client.headers.update({'Origin': origin, 'X-CSRF-Token': session.json()['csrf']})
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def completed(client, run_id):
    response = client.get('/api/runs/' + run_id, params={'activity': 'summary'})
    response.raise_for_status()
    run = response.json()
    return run if run['status'] in {'idle', 'completed'} else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local Postgres database.')
    schema = 'moyai_demo_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-runtime-demo-') as temporary:
            directory = Path(temporary)
            print('START  First Moyai server process is starting with PostgreSQL 17.', flush=True)
            with server(url, schema, directory, initialize=True) as client:
                response = client.post('/api/runs', json={'prompt': 'Synthetic Postgres demo — 日本語 🗿',
                                                         'mode': 'demo', 'chat_enabled': True})
                response.raise_for_status()
                run_id = response.json()['id']
                run = wait_for(lambda: completed(client, run_id))
                messages = run['messages']
                assert any(m['role'] == 'assistant' and m['content'] for m in messages)
                assert run['activity_cursor'] > 0
                print(f"CHAT   Real HTTP request completed; {len(messages)} messages persisted in Postgres.", flush=True)
                with psycopg.connect(url) as conn:
                    count = conn.execute(f'SELECT count(*) FROM "{schema}".messages').fetchone()[0]
                assert count == len(messages)
                assert not (directory / 'workspace.db').exists()
                print('CHECK  Direct database read matches the API. No SQLite database was created.', flush=True)
            print('STOP   First server process exited; its database connections are closed.', flush=True)
            with server(url, schema, directory, initialize=False) as client:
                restored = completed(client, run_id)
                assert restored and restored['messages'] == messages
                print('START  Second server process restored the same chat and saved answer.', flush=True)
                response = client.post(f'/api/runs/{run_id}/messages',
                                       json={'content': 'Continue after restart', 'client_id': 'demo-followup'})
                response.raise_for_status()
                final = wait_for(lambda: completed(client, run_id))
                assert len(final['messages']) == len(messages) + 2
                print('WRITE  Follow-up completed after restart; both turns remain in the same chat.', flush=True)
                report = {'backend': 'postgresql', 'server_processes': 2, 'http_api_verified': True,
                          'messages_before_restart': len(messages), 'messages_after_restart': len(final['messages']),
                          'sqlite_database_created': False, 'production_cutover': False}
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, indent=2) + '\n')
            print('PASS   Postgres runtime and restart verified. Production cutover is a separate step.', flush=True)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('FAIL   Runtime demo failed. Run the Postgres tests for diagnostics.', file=sys.stderr)
        raise SystemExit(1)
