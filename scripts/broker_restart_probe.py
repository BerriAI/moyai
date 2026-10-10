"""Real HTTP/Temporal/Postgres probe: keep native inference across an API restart.

The upstream model is a local streaming fixture. No cloud sandbox, S3 payload or
paid model call is made. API and broker are separate, unmodified server processes.
"""
import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import psycopg
from temporalio.testing import WorkflowEnvironment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Store
from app.security import digest


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if result := predicate():
                return result
        except httpx.TransportError:
            pass
        time.sleep(.05)
    raise TimeoutError('Local broker probe did not reach the expected state.')


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError('API did not stop cleanly.') from None


@contextmanager
def server(environment, role, directory):
    directory.mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
            '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error'], cwd=directory,
            env={**environment, 'MOYAI_RUNTIME_ROLE': role, 'DATA_DIR': str(directory)},
            stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=10) as client:
                wait_for(lambda: client.get('/health').status_code == 200)
                yield process, client
        finally:
            if process.poll() is None:
                stop(process)


def probe(url, temporal_address, output, hold_seconds):
    started, events = time.monotonic(), []

    def record(message):
        print(message, flush=True)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])

    release, first_chunk = threading.Event(), threading.Event()
    provider_calls = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            provider_calls.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def event(kind, **data):
                self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n').encode())
                self.wfile.flush()

            event('response.output_text.delta', delta='before-api-restart')
            if not release.wait(60):
                return
            event('response.output_text.delta', delta='after-api-restart')
            event('response.completed', response={'id': 'synthetic-response', 'status': 'completed',
                  'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 4, 'total_tokens': 6}})

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    schema = 'moyai_broker_probe_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    report = {}
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-broker-probe-') as temporary:
            root = Path(temporary)
            environment = {
                'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'PYTHONUNBUFFERED': '1',
                'PUBLIC_URL': 'http://127.0.0.1:8787', 'SESSION_SECRET': 'synthetic-broker-probe-session',
                'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'AGENT_HARNESS': 'hermes',
                'AGENT_MODEL': 'openai/gpt-6-astra',
                'MODEL_CONTEXT_LIMITS': json.dumps({'openai/gpt-6-astra': {
                    'context_window': 128000, 'max_input_tokens': 120000, 'max_output_tokens': 8000}}),
                'MOYAI_DATABASE_URL': url, 'MOYAI_DATABASE_SCHEMA': schema,
                'MOYAI_DATABASE_INITIALIZE': 'true', 'MOYAI_BUILD_SHA': 'a' * 40,
                'MOYAI_SEPARATE_BROKER': 'true', 'OBJECT_STORAGE_BUCKET': 'synthetic-broker-probe',
                'OBJECT_STORAGE_ACCESS_KEY_ID': 'synthetic', 'OBJECT_STORAGE_SECRET_ACCESS_KEY': 'synthetic',
                'TEMPORAL_ENABLED': 'true', 'TEMPORAL_TLS': 'false', 'TEMPORAL_ADDRESS': temporal_address,
                'TEMPORAL_TASK_QUEUE': 'broker-probe-' + uuid4().hex,
                'SESSION_TITLES_ENABLED': 'false', 'MEMORY_REVIEW_ENABLED': 'false',
                'LITELLM_SPEND_RECOVERY_ENABLED': 'false', 'MAX_CONCURRENT_MODEL_REQUESTS': '1',
                'LITELLM_API_BASE': f'http://127.0.0.1:{upstream.server_port}/v1',
                'LITELLM_API_KEY': 'synthetic-local-key',
            }
            record('START  Real PostgreSQL, Temporal, API and broker processes; local model fixture.')
            with server(environment, 'coordinator', root / 'api') as (api_process, api):
                with server(environment, 'broker', root / 'broker') as (broker_process, broker):
                    store = Store(root / 'observer', database_url=url, database_schema=schema)
                    try:
                        run = store.create_run('Synthetic broker continuity', '', 'modal', [], model='openai/gpt-6-astra')
                        # No execution worker is started. The durable journal
                        # makes this an existing session on API recovery.
                        store.execute("INSERT INTO durable_sessions(run_id,state,revision,delivered) VALUES(?,'{}',0,0)", (run['id'],))
                        store.update_run(run['id'], status='running', token_hash=digest('synthetic-capability'))
                        route = f"/broker/{run['id']}/v1/responses"
                        headers = {'Authorization': 'Bearer synthetic-capability'}
                        body = {'input': 'Synthetic continuity check', 'stream': True}
                        received, failures = [], []

                        def consume():
                            try:
                                with httpx.Client(timeout=70) as stream_client:
                                    with stream_client.stream('POST', str(broker.base_url).rstrip('/') + route,
                                                              headers=headers, json=body) as response:
                                        if response.is_error:
                                            response.read()
                                            raise RuntimeError(f'HTTP {response.status_code}: {response.text}')
                                        for line in response.iter_lines():
                                            if line.startswith('data: '):
                                                item = json.loads(line[6:])
                                                received.append(item)
                                                if item.get('delta') == 'before-api-restart':
                                                    first_chunk.set()
                            except RuntimeError as exc:
                                failures.append(str(exc))
                                first_chunk.set()
                            except Exception as exc:
                                failures.append(type(exc).__name__)
                                first_chunk.set()

                        stream = threading.Thread(target=consume, daemon=True)
                        stream.start()
                        try:
                            assert first_chunk.wait(15) and not failures, f'Stream never reached the client: {failures}'
                            record('STREAM Received first native Responses token from the broker.')
                            assert api.post(route, headers=headers, json=body).status_code == 503
                            assert broker.get('/api/session').status_code == 404
                            assert broker.post(route, json=body).status_code == 401
                            queued = broker.post(route, headers=headers, json=body)
                            assert queued.status_code == 429 and queued.headers['x-moyai-model-queue'] == '1'
                            assert len(provider_calls) == 1
                            record('CHECK  API rejects inference; broker enforces run auth and the one-call limit.')
                            stop(api_process)
                            assert broker_process.poll() is None
                            assert store.rows('SELECT status FROM model_requests')[0]['status'] == 'pending'
                            record('STOP   API process exited. Broker and original model request remain alive.')
                            with server(environment, 'coordinator', root / 'replacement') as (_, replacement):
                                assert replacement.get('/health').status_code == 200
                                assert store.rows('SELECT status FROM model_requests')[0]['status'] == 'pending'
                                assert broker.post(route, headers=headers, json=body).status_code == 429
                                record('START  Replacement API is healthy; the same request still owns its slot.')
                                # Optional provider hold makes the real recording readable.
                                if hold_seconds:
                                    time.sleep(hold_seconds)
                                release.set()
                                stream.join(timeout=15)
                                assert not stream.is_alive() and not failures
                                assert [v['delta'] for v in received if 'delta' in v] == [
                                    'before-api-restart', 'after-api-restart']
                                assert received[-1]['type'] == 'response.completed'
                                wait_for(lambda: store.rows('SELECT status FROM model_requests')[0]['status'] == 'completed')
                                assert len(provider_calls) == 1
                                record('PASS   Same stream completed after API restart; one provider call, one completed ledger row.')
                                report = {'api_restarted': True, 'broker_pid_unchanged': True,
                                    'native_stream_preserved': True, 'provider_calls': 1,
                                    'capacity_enforced_during_restart': True, 'request_status': 'completed',
                                    'real_postgres': True, 'real_temporal': True, 'upstream': 'local_fixture',
                                    'same_build': True, 'production_changed': False}
                        finally:
                            release.set()
                            stream.join(timeout=15)
                    finally:
                        store.close()
    finally:
        release.set()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        if output:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            header = {'version': 2, 'width': 112, 'height': 18, 'timestamp': int(time.time()),
                      'title': 'Moyai: real broker continuity across an API restart'}
            (output / 'broker-restart.cast').write_text('\n'.join(json.dumps(e) for e in [header, *events]) + '\n')
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--hold-seconds', type=float, default=0)
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local PostgreSQL database.')
    if not 0 <= args.hold_seconds <= 10:
        parser.error('Use --hold-seconds between 0 and 10.')
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as temporal:
        await asyncio.to_thread(probe, url, temporal.client.service_client.config.target_host,
                                args.output, args.hold_seconds)


if __name__ == '__main__':
    asyncio.run(main())
