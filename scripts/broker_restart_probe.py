"""Real HTTP/Temporal/Postgres probe: keep native inference across an API restart.

The upstream model is a local streaming fixture. No cloud sandbox, S3 payload or
paid model call is made. API and broker are separate server processes. The optional
mixed-build rehearsal copies the source and adds a synthetic read-only API route;
it proves this specific compatible change, not arbitrary cross-release safety.
"""
import argparse
import asyncio
from contextlib import contextmanager, ExitStack, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
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


DRAIN_FIXTURE = '''
import asyncio, os
from pathlib import Path
import uvicorn
from fastapi.responses import StreamingResponse
from app.main import app
from app.db import database
from render_start import API_GRACEFUL_SHUTDOWN_SECONDS

@app.get('/__probe__/drain')
async def admitted_request():
    async def body():
        yield 'admitted\\n'
        async with asyncio.timeout(30):
            while not Path(os.environ['PROBE_DRAIN_GATE']).exists():
                await asyncio.sleep(.05)
        await database(app.state.store.rows, 'SELECT 1')
        yield 'finished-after-sigterm\\n'
    return StreamingResponse(body(), media_type='text/plain')

uvicorn.run(app, host='127.0.0.1', port=int(os.environ['PROBE_PORT']),
            log_level='error', timeout_graceful_shutdown=API_GRACEFUL_SHUTDOWN_SECONDS)
'''


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
        command = [sys.executable, '-m', 'uvicorn', 'app.main:app',
                   '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error']
        if role == 'api' and environment.get('PROBE_DRAIN_GATE'):
            command = [sys.executable, '-c', DRAIN_FIXTURE]
        process = subprocess.Popen(command, cwd=directory,
            env={**environment, 'MOYAI_RUNTIME_ROLE': role, 'DATA_DIR': str(directory),
                 'PROBE_PORT': str(port), 'PYTHONPATH': environment.get('PYTHONPATH', '') + os.pathsep + str(ROOT)},
            stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=10) as client:
                wait_for(lambda: client.get('/health').status_code == 200)
                yield process, client
        finally:
            if process.poll() is None:
                stop(process)


def source_build(root):
    digest = hashlib.sha256()
    for directory in ('app', 'agent', 'sandbox'):
        for path in sorted((root / directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                digest.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()[:40]


def candidate_builds(root, environment):
    candidate = root / 'candidate-source'
    for directory in ('app', 'agent', 'sandbox'):
        shutil.copytree(ROOT / directory, candidate / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    main = candidate / 'app/main.py'
    main.write_text(main.read_text() + '''

@app.get('/__probe__/api-release')
async def api_release_probe():
    return {'release': 'compatible-candidate'}
''')
    compatible = {**environment, 'PYTHONPATH': str(candidate), 'MOYAI_BUILD_SHA': source_build(candidate)}
    incompatible = root / 'incompatible-source'
    shutil.copytree(candidate, incompatible)
    contract = incompatible / 'app/runtime_compatibility.py'
    source = contract.read_text()
    from app.runtime_compatibility import API_PROTOCOL_REVISION
    contract.write_text(source.replace(f'API_PROTOCOL_REVISION = {API_PROTOCOL_REVISION}',
                                       f'API_PROTOCOL_REVISION = {API_PROTOCOL_REVISION + 1}'))
    rejected = {**environment, 'PYTHONPATH': str(incompatible), 'MOYAI_BUILD_SHA': source_build(incompatible)}
    return compatible, rejected


def reject_incompatible(environment, directory):
    directory.mkdir()
    result = subprocess.run([sys.executable, '-m', 'uvicorn', 'app.main:app', '--port', '0'],
        cwd=directory, env={**environment, 'MOYAI_RUNTIME_ROLE': 'api', 'DATA_DIR': str(directory)},
        capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and 'API requires a compatible protocol' in result.stderr, 'Incompatible API did not fail closed.'


def probe(url, temporal_address, output, hold_seconds, replicated_api=False, mixed_build_api=False, drain_api=False):
    replicated_api = replicated_api or mixed_build_api
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
            candidate_environment, rejected_environment = environment, None
            if drain_api:
                environment['PROBE_DRAIN_GATE'] = str(root / 'finish-admitted-request')
            if mixed_build_api:
                environment['MOYAI_BUILD_SHA'] = source_build(ROOT)
                # Set verify before copying environments for both candidates.
                environment['MOYAI_SCHEMA_MODE'] = 'verify'
                candidate_environment, rejected_environment = candidate_builds(root, environment)
                record('BUILD  Candidate adds a read-only fixture route; source build IDs differ.')
            with ExitStack() as lifetime:
                coordinator_process = None
                if replicated_api:
                    environment['MOYAI_SCHEMA_MODE'] = 'verify'
                    migrated = subprocess.run([sys.executable, '-m', 'app.schema_migrations', '--apply'],
                        cwd=root, env={**environment, 'MOYAI_RUNTIME_ROLE': 'coordinator'}, capture_output=True, text=True, timeout=30)
                    assert migrated.returncode == 0, 'Offline fixture migration failed.'
                    coordinator_process, _ = lifetime.enter_context(server(environment, 'coordinator', root / 'coordinator'))
                role = 'api' if replicated_api else 'coordinator'
                api_process, api = lifetime.enter_context(server(environment, role, root / 'api'))
                with server(environment, 'broker', root / 'broker') as (broker_process, broker):
                    store = Store(root / 'observer', database_url=url, database_schema=schema,
                                  schema_mode='verify' if replicated_api else 'auto')
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

                        traffic_stop, traffic_failures, traffic_samples = threading.Event(), [], []
                        selected_api = [str(api.base_url).rstrip('/')]
                        traffic = None

                        def requests_during_handoff():
                            with httpx.Client(timeout=5, cookies=api.cookies) as client:
                                while not traffic_stop.is_set():
                                    start = time.monotonic()
                                    try:
                                        response = client.get(selected_api[0] + '/api/runs/' + run['id'])
                                        assert response.status_code == 200 and response.json()['id'] == run['id']
                                        traffic_samples.append(round((time.monotonic() - start) * 1000, 2))
                                    except Exception as exc:
                                        traffic_failures.append(type(exc).__name__)
                                    traffic_stop.wait(.05)

                        stream = threading.Thread(target=consume, daemon=True)
                        stream.start()
                        admitted, drained, drain_failures = threading.Event(), [], []
                        drain_thread = None
                        def admitted_request():
                            try:
                                with httpx.Client(timeout=35) as client:
                                    with client.stream('GET', str(api.base_url).rstrip('/') + '/__probe__/drain') as response:
                                        assert response.status_code == 200
                                        for line in response.iter_lines():
                                            drained.append(line)
                                            if line == 'admitted': admitted.set()
                            except Exception as exc:
                                drain_failures.append(type(exc).__name__)
                                admitted.set()
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
                            replacement = None
                            if replicated_api:
                                assert api.get('/api/session').json()['authenticated']
                                if drain_api:
                                    drain_thread = threading.Thread(target=admitted_request, daemon=True)
                                    drain_thread.start()
                                    assert admitted.wait(10) and not drain_failures
                                    record('ADMIT  Old API started a finite streaming HTTP request before replacement.')
                                traffic = threading.Thread(target=requests_during_handoff, daemon=True)
                                traffic.start()
                                replacement_process, replacement = lifetime.enter_context(server(candidate_environment, 'api', root / 'replica'))
                                replacement.cookies.update(api.cookies)
                                assert replacement.get('/api/runs/' + run['id']).json()['id'] == run['id']
                                assert api.get('/health').status_code == replacement.get('/health').status_code == 200
                                assert replacement.post('/hooks/slack/events', json={}).status_code == 503
                                assert coordinator_process.poll() is None
                                record('READY  Two API replicas serve the same signed session; coordinator stays alive.')
                                if mixed_build_api:
                                    assert api.get('/__probe__/api-release').status_code == 404
                                    assert replacement.get('/__probe__/api-release').json() == {'release': 'compatible-candidate'}
                                    assert environment['MOYAI_BUILD_SHA'] != candidate_environment['MOYAI_BUILD_SHA']
                                    record('CHECK  Old API: fixture route 404. New API: compatible-candidate 200.')
                                    reject_incompatible(rejected_environment, root / 'rejected')
                                    record('REJECT Incompatible protocol exits before readiness; live inference stays pending.')
                                selected_api[0] = str(replacement.base_url).rstrip('/')
                                record('ROUTE  New HTTP requests now go to the ready replacement API.')
                            if drain_api:
                                api_process.terminate()
                                time.sleep(.3)
                                assert api_process.poll() is None and drain_thread.is_alive()
                                assert replacement.get('/health').status_code == 200
                                record('DRAIN  Old API received SIGTERM and stays alive; replacement serves new requests.')
                                Path(environment['PROBE_DRAIN_GATE']).touch()
                                drain_thread.join(timeout=10)
                                assert not drain_thread.is_alive() and not drain_failures
                                assert drained == ['admitted', 'finished-after-sigterm']
                                api_process.wait(timeout=15)
                                record('PASS   Admitted API request completed its database read after SIGTERM; old process exited.')
                            else:
                                stop(api_process)
                            assert broker_process.poll() is None
                            assert store.rows('SELECT status FROM model_requests')[0]['status'] == 'pending'
                            record('STOP   API process exited. Broker and original model request remain alive.')
                            replacement_context = (nullcontext((replacement_process, replacement)) if replicated_api
                                                   else server(environment, 'coordinator', root / 'replacement'))
                            with replacement_context as (_, replacement):
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
                                if replicated_api:
                                    traffic_stop.set()
                                    traffic.join(timeout=10)
                                    assert not traffic.is_alive() and not traffic_failures and traffic_samples
                                    assert coordinator_process.poll() is None
                                    record(f'PASS   {len(traffic_samples)} authenticated HTTP reads, zero errors; coordinator PID unchanged.')
                                report = {'api_restarted': True, 'broker_pid_unchanged': True,
                                    'native_stream_preserved': True, 'provider_calls': 1,
                                    'capacity_enforced_during_restart': True, 'request_status': 'completed',
                                    'real_postgres': True, 'real_temporal': True, 'upstream': 'local_fixture',
                                    'same_build': not mixed_build_api, 'production_changed': False,
                                    'mixed_build_api': mixed_build_api,
                                    'baseline_build': environment['MOYAI_BUILD_SHA'],
                                    'candidate_build': candidate_environment['MOYAI_BUILD_SHA'],
                                    'incompatible_protocol_rejected': mixed_build_api,
                                    'source_change': 'synthetic_read_only_route' if mixed_build_api else 'none',
                                    'api_replicas_overlapped': replicated_api,
                                    'admitted_api_request_drained': drain_api,
                                    'coordinator_pid_unchanged': bool(coordinator_process and coordinator_process.poll() is None),
                                    'http_reads': len(traffic_samples), 'http_errors': traffic_failures,
                                    'http_max_latency_ms': max(traffic_samples, default=0)}
                        finally:
                            traffic_stop.set()
                            if traffic:
                                traffic.join(timeout=10)
                            if drain_api:
                                Path(environment['PROBE_DRAIN_GATE']).touch()
                                if drain_thread: drain_thread.join(timeout=10)
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
            name = 'api-drain.cast' if drain_api else ('mixed-build-api.cast' if mixed_build_api else ('api-overlap.cast' if replicated_api else 'broker-restart.cast'))
            (output / name).write_text('\n'.join(json.dumps(e) for e in [header, *events]) + '\n')
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--hold-seconds', type=float, default=0)
    parser.add_argument('--replicated-api', action='store_true',
        help='Migrate the fixture schema and overlap two API replicas under a separate coordinator.')
    parser.add_argument('--mixed-build-api', action='store_true',
        help='Overlap source builds differing by a fixture API route, then reject an incompatible protocol.')
    parser.add_argument('--drain-api', action='store_true',
        help='With --mixed-build-api, hold an admitted HTTP request across SIGTERM and verify it drains.')
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local PostgreSQL database.')
    if not 0 <= args.hold_seconds <= 10:
        parser.error('Use --hold-seconds between 0 and 10.')
    if args.drain_api and not args.mixed_build_api:
        parser.error('--drain-api requires --mixed-build-api.')
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as temporal:
        await asyncio.to_thread(probe, url, temporal.client.service_client.config.target_host,
                                args.output, args.hold_seconds, args.replicated_api, args.mixed_build_api, args.drain_api)


if __name__ == '__main__':
    asyncio.run(main())
