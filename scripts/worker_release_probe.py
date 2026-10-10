"""Real worker processes, PostgreSQL and Temporal under continuous HTTP submissions.

Workers run Moyai's demo mode; the long model stream uses a local HTTP fixture.
The candidate changes demo response text. No cloud, S3 or paid model is used.
This is a protocol/handoff rehearsal, not a Render deployment controller.
"""
import argparse
import asyncio
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import hmac
import os
from pathlib import Path
import shutil
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
from temporalio.worker import Replayer

from app.db import Store
from app.security import digest
from app.session_workflow import SessionWorkflow
from scripts.broker_restart_probe import ROOT, server, source_build, stop, wait_for

WORKER_FIXTURE = '''
import asyncio, os
from pathlib import Path
import uvicorn
from app.main import app
manager = app.state.manager
step = manager.step
async def held_step(run_id, state):
    if app.state.store.run(run_id)['prompt'] == os.environ.get('PROBE_HOLD_PROMPT'):
        Path(os.environ['PROBE_HELD']).touch()
        await asyncio.Event().wait()
    return await step(run_id, state)
manager.step = held_step
uvicorn.run(app, host='127.0.0.1', port=int(os.environ['PROBE_PORT']), log_level='error')
'''


@contextmanager
def worker(environment, directory, hold_prompt=''):
    directory.mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([sys.executable, '-c', WORKER_FIXTURE], cwd=directory,
            env={**environment, 'MOYAI_RUNTIME_ROLE': 'worker', 'DATA_DIR': str(directory),
                 'PROBE_PORT': str(port), 'PROBE_HOLD_PROMPT': hold_prompt,
                 'PROBE_HELD': str(directory / 'held')}, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5) as client:
                def healthy():
                    assert process.poll() is None, 'Worker exited before readiness'
                    return client.get('/health').status_code == 200
                wait_for(healthy)
                yield process, client
        finally:
            if process.poll() is None:
                stop(process)


def candidate_sources(root, environment):
    candidate = root / 'candidate-source'
    for directory in ('app', 'agent', 'sandbox'):
        shutil.copytree(ROOT / directory, candidate / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    runner = candidate / 'app/runner.py'
    text = runner.read_text()
    assert text.count('Demo complete. Your task and activity are saved.') == 1
    runner.write_text(text.replace('Demo complete. Your task and activity are saved.',
                                  'Compatible worker fixture. Your task and activity are saved.'))
    compatible = {**environment, 'PYTHONPATH': str(candidate), 'MOYAI_BUILD_SHA': source_build(candidate)}
    incompatible = root / 'incompatible-source'
    shutil.copytree(candidate, incompatible)
    contract = incompatible / 'app/runtime_compatibility.py'
    from app.runtime_compatibility import WORKER_PROTOCOL_REVISION
    contract.write_text(contract.read_text().replace(f'WORKER_PROTOCOL_REVISION = {WORKER_PROTOCOL_REVISION}',
                                                    f'WORKER_PROTOCOL_REVISION = {WORKER_PROTOCOL_REVISION + 1}'))
    rejected = {**environment, 'PYTHONPATH': str(incompatible), 'MOYAI_BUILD_SHA': source_build(incompatible)}
    return compatible, rejected


def probe(url, temporal_address, output, all_roles=False):
    started, events = time.monotonic(), []
    report = {'status': 'failed', 'production_changed': False}

    def record(message):
        print(message, flush=True)
        events.append([round(time.monotonic() - started, 3), 'o', message + '\r\n'])

    release, received_first, traffic_stop = threading.Event(), threading.Event(), threading.Event()
    calls, deltas, failures, submissions, latencies = [], [], [], [], []
    stream = traffic = None

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(self.path)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def event(kind, **data):
                self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **data}) + '\n\n').encode())
                self.wfile.flush()

            event('response.output_text.delta', delta='before-worker-upgrade')
            if not release.wait(120):
                return
            event('response.output_text.delta', delta='after-worker-rollback')
            event('response.completed', response={'id': 'fixture-response', 'status': 'completed',
                  'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 4, 'total_tokens': 6}})

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    provider.daemon_threads = True
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    schema = 'moyai_worker_probe_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-worker-probe-') as temporary, ExitStack() as lifetime:
            root = Path(temporary)
            environment = {
                'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'PYTHONUNBUFFERED': '1',
                'PUBLIC_URL': 'http://127.0.0.1:8787', 'SESSION_SECRET': 'synthetic-worker-probe-session',
                'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'AGENT_HARNESS': 'hermes',
                'AGENT_MODEL': 'openai/gpt-6-astra', 'DEMO_STEP_SECONDS': '.01',
                'MODEL_CONTEXT_LIMITS': json.dumps({'openai/gpt-6-astra': {
                    'context_window': 128000, 'max_input_tokens': 120000, 'max_output_tokens': 8000}}),
                'MOYAI_DATABASE_URL': url, 'MOYAI_DATABASE_SCHEMA': schema,
                'MOYAI_DATABASE_INITIALIZE': 'true', 'MOYAI_SCHEMA_MODE': 'verify',
                'MOYAI_BUILD_SHA': source_build(ROOT), 'MOYAI_SEPARATE_BROKER': 'true',
                'OBJECT_STORAGE_BUCKET': 'synthetic-worker-probe',
                'OBJECT_STORAGE_ACCESS_KEY_ID': 'synthetic', 'OBJECT_STORAGE_SECRET_ACCESS_KEY': 'synthetic',
                'TEMPORAL_ENABLED': 'true', 'TEMPORAL_TLS': 'false', 'TEMPORAL_ADDRESS': temporal_address,
                'TEMPORAL_TASK_QUEUE': 'worker-probe-' + uuid4().hex,
                'TEMPORAL_WORKER_ACTIVITIES': '1',
                'SESSION_TITLES_ENABLED': 'false', 'MEMORY_REVIEW_ENABLED': 'false',
                'LITELLM_SPEND_RECOVERY_ENABLED': 'false', 'MAX_CONCURRENT_MODEL_REQUESTS': '1',
                'LITELLM_API_BASE': f'http://127.0.0.1:{provider.server_port}/v1',
                'LITELLM_API_KEY': 'synthetic-local-key',
            }
            if all_roles:
                environment.update(MOYAI_COORDINATOR_HANDOFF='true',
                                   SLACK_SIGNING_SECRET='synthetic-rolling-probe')
            candidate, rejected = candidate_sources(root, environment)
            assert candidate['MOYAI_BUILD_SHA'] != environment['MOYAI_BUILD_SHA']
            migrated = subprocess.run([sys.executable, '-m', 'app.schema_migrations', '--apply'],
                cwd=root, env={**environment, 'MOYAI_RUNTIME_ROLE': 'coordinator'}, capture_output=True, timeout=30)
            assert migrated.returncode == 0, 'Fixture migration failed'
            record('START  Real PostgreSQL, Temporal, coordinator, API, broker and worker processes.')
            control, control_http = lifetime.enter_context(server(environment, 'coordinator', root / 'coordinator'))
            if all_roles:
                wait_for(lambda: control_http.get('/health').headers.get('x-moyai-coordinator-state') == 'leader')
            api_process, api = lifetime.enter_context(server(environment, 'api', root / 'api'))
            broker_process, broker = lifetime.enter_context(server(environment, 'broker', root / 'broker'))
            store = Store(root / 'observer', database_url=url, database_schema=schema, schema_mode='verify')
            lifetime.callback(store.close)
            policy_tables = ('runtime_policy', 'runtime_api_policy', 'runtime_worker_policy', 'runtime_broker_policy')
            if all_roles:
                policy_tables += ('runtime_coordinator_policy',)
            policies = {table: store.rows('SELECT * FROM ' + table) for table in policy_tables}
            api.headers.update({'Origin': environment['PUBLIC_URL'], 'X-CSRF-Token': api.get('/api/session').json()['csrf']})

            def submit(client, prompt):
                before = time.monotonic()
                response = client.post('/api/runs', json={'prompt': prompt, 'mode': 'demo', 'chat_enabled': True})
                assert response.status_code == 201, f'Submission returned HTTP {response.status_code}'
                latencies.append(round((time.monotonic() - before) * 1000, 2))
                run_id = response.json()['id']
                submissions.append(run_id)
                return run_id

            def answer(run_id):
                rows = [m for m in store.messages(run_id) if m['role'] == 'assistant']
                return rows[0]['content'] if len(rows) == 1 and store.run(run_id)['status'] == 'idle' else None

            old_run = submit(api, 'Hold old worker')
            old, old_http = lifetime.enter_context(worker(environment, root / 'old-worker', 'Hold old worker'))
            wait_for(lambda: (root / 'old-worker/held').exists())
            initial_message = store.run(old_run)['active_message_id']
            record('OWNED  Old worker holds a real Temporal activity and PostgreSQL session lease.')

            inference = store.create_run('Synthetic inference stream', '', 'modal', [], model='openai/gpt-6-astra')
            store.execute("INSERT INTO durable_sessions(run_id,state,revision,delivered) VALUES(?,'{}',0,0)", (inference['id'],))
            store.update_run(inference['id'], status='running', token_hash=digest('synthetic-capability'))
            route = f"/broker/{inference['id']}/v1/responses"
            auth = {'Authorization': 'Bearer synthetic-capability'}
            body = {'input': 'Synthetic continuity check', 'stream': True}

            def consume():
                try:
                    with httpx.Client(timeout=130) as client, client.stream('POST', str(broker.base_url).rstrip('/') + route,
                                                                           headers=auth, json=body) as response:
                        assert response.status_code == 200
                        for line in response.iter_lines():
                            if line.startswith('data: '):
                                item = json.loads(line[6:])
                                deltas.append(item)
                                if item.get('delta') == 'before-worker-upgrade':
                                    received_first.set()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    received_first.set()

            def new_messages():
                try:
                    with httpx.Client(base_url=api.base_url, cookies=api.cookies, headers=api.headers, timeout=10) as client:
                        count = 0
                        while not traffic_stop.is_set():
                            count += 1
                            submit(client, f'Continuous submission {count}')
                            traffic_stop.wait(.2)
                except Exception as exc:
                    failures.append(type(exc).__name__)

            stream = threading.Thread(target=consume, daemon=True)
            stream.start()
            lifetime.callback(release.set)
            lifetime.callback(traffic_stop.set)
            assert received_first.wait(15) and not failures
            record('STREAM Broker delivered first token; new messages continue arriving through the API.')
            traffic = threading.Thread(target=new_messages, daemon=True)
            traffic.start()
            new, new_http = lifetime.enter_context(worker(candidate, root / 'new-worker', 'Hold candidate worker'))
            assert old_http.get('/health').status_code == new_http.get('/health').status_code == 200
            new_run = submit(api, 'Candidate processes new work while old turn is held')
            assert 'Compatible worker fixture' in wait_for(lambda: answer(new_run))
            record('READY  Different worker builds overlap; candidate completes new work while old owns its turn.')
            rejected_result = subprocess.run([sys.executable, '-c', 'import app.main'], cwd=root,
                env={**rejected, 'MOYAI_RUNTIME_ROLE': 'worker', 'DATA_DIR': str(root / 'rejected')},
                capture_output=True, text=True, timeout=15)
            assert rejected_result.returncode != 0 and 'Worker requires a compatible protocol' in rejected_result.stderr
            record('REJECT Incompatible worker protocol fails before readiness; active stream remains pending.')
            stop(old)
            assert 'Compatible worker fixture' in wait_for(lambda: answer(old_run), timeout=45)
            assert store.messages(old_run)[0]['id'] == initial_message
            record('PASS   Old worker exits; candidate resumes its saved activity and publishes one answer.')
            assert store.rows('SELECT status FROM model_requests')[0]['status'] == 'pending'
            assert broker.post(route, headers=auth, json=body).status_code == 429
            rollback_run = submit(api, 'Hold candidate worker')
            wait_for(lambda: (root / 'new-worker/held').exists())
            rollback, _ = lifetime.enter_context(worker(environment, root / 'rollback-worker'))
            stop(new)
            assert 'Demo complete.' in wait_for(lambda: answer(rollback_run), timeout=45)
            record('PASS   Previous build resumes candidate-admitted work; rollback keeps accepting messages.')
            traffic_stop.set()
            traffic.join(timeout=12)
            assert not traffic.is_alive() and not failures
            wait_for(lambda: all(answer(run_id) for run_id in submissions), timeout=45)
            assert all(sum(m['role'] == 'assistant' for m in store.messages(run_id)) == 1 for run_id in submissions)
            assert {table: store.rows('SELECT * FROM ' + table) for table in policy_tables} == policies
            assert all(process.poll() is None for process in (control, api_process, broker_process, rollback))
            if all_roles:
                # Resume continuous submissions for the rest of the release.
                traffic_stop.clear()
                traffic = threading.Thread(target=new_messages, daemon=True)
                traffic.start()
                new_broker, new_broker_http = lifetime.enter_context(server(candidate, 'broker', root / 'new-broker'))
                assert new_broker_http.post(route, headers=auth, json=body).status_code == 429
                assert store.rows('SELECT status FROM model_requests')[0]['status'] == 'pending'
                broker_process.terminate()
                time.sleep(.25)
                assert broker_process.poll() is None, 'Old broker exited before admitted stream drained'
                record('BROKER Replacement is ready; shared capacity stays at one while the old stream drains.')
            release.set()
            stream.join(timeout=15)
            assert not stream.is_alive() and not failures
            assert [v['delta'] for v in deltas if 'delta' in v] == ['before-worker-upgrade', 'after-worker-rollback']
            assert deltas[-1]['type'] == 'response.completed' and len(calls) == 1
            wait_for(lambda: store.rows('SELECT status FROM model_requests')[0]['status'] == 'completed')
            if all_roles:
                broker_process.wait(timeout=15)
                assert broker_process.returncode == 0
                response = new_broker_http.post(route, headers=auth, json=body)
                assert response.status_code == 200 and 'response.completed' in response.text
                assert len(calls) == 2
                record('PASS   Old broker exits after its final token; replacement serves the next inference.')
                new_control, new_control_http = lifetime.enter_context(server(candidate, 'coordinator', root / 'new-coordinator'))
                assert new_control_http.get('/health').headers['x-moyai-coordinator-state'] == 'standby'

                def hook(client, number):
                    payload = json.dumps({'type': 'event_callback', 'team_id': 'PROBE',
                        'event_id': 'EV' + str(number), 'event': {'type': 'synthetic_probe'}}).encode()
                    timestamp = str(int(time.time()))
                    signature = hmac.new(b'synthetic-rolling-probe', b'v0:' + timestamp.encode() + b':' + payload,
                                         hashlib.sha256).hexdigest()
                    result = client.post('/hooks/slack/events', content=payload, headers={
                        'X-Slack-Request-Timestamp': timestamp, 'X-Slack-Signature': 'v0=' + signature,
                        'Content-Type': 'application/json'})
                    assert result.status_code == 200, result.status_code

                for number in range(10):
                    hook(new_control_http, number)
                stop(control)
                wait_for(lambda: new_control_http.get('/health').headers.get('x-moyai-coordinator-state') == 'leader')
                for number in range(10, 20):
                    hook(new_control_http, number)
                wait_for(lambda: store.rows("SELECT COUNT(*) AS n FROM coordinator_hooks WHERE status='done'")[0]['n'] == 20)
                record('PASS   Standby accepts signed hooks; old coordinator drains and transfers leadership.')
                new_api, new_api_http = lifetime.enter_context(server(candidate, 'api', root / 'new-api'))
                new_api_http.cookies.update(api.cookies)
                new_api_http.headers.update(api.headers)
                traffic_stop.set()
                traffic.join(timeout=12)
                assert not traffic.is_alive() and not failures
                # Switch the test client's target once replacement health is
                # verified, as Render's stable service routing does.
                api = new_api_http
                traffic_stop.clear()
                traffic = threading.Thread(target=new_messages, daemon=True)
                traffic.start()
                stop(api_process)
                final_worker, _ = lifetime.enter_context(worker(candidate, root / 'final-worker'))
                stop(rollback)
                final_run = submit(api, 'Final compatible release submission')
                assert 'Compatible worker fixture' in wait_for(lambda: answer(final_run))
                traffic_stop.set()
                traffic.join(timeout=12)
                assert not traffic.is_alive() and not failures
                wait_for(lambda: all(answer(run_id) for run_id in submissions), timeout=45)
                assert all(sum(m['role'] == 'assistant' for m in store.messages(run_id)) == 1 for run_id in submissions)
                assert {table: store.rows('SELECT * FROM ' + table) for table in policy_tables} == policies
                assert all(process.poll() is None for process in (new_control, new_api, new_broker, final_worker))
                record('PASS   API traffic switches after readiness; all four replacements are live on the candidate build.')
            record(f'PASS   {len(submissions)} HTTP submissions, zero errors, one answer each; one uninterrupted provider call.')
            report.update(status='replay_pending', real_postgres=True, real_temporal=True,
                worker_processes=True, source_change='synthetic_demo_response_text',
                baseline_build=environment['MOYAI_BUILD_SHA'], candidate_build=candidate['MOYAI_BUILD_SHA'],
                incompatible_protocol_rejected=True, rollback_resumed_candidate_work=True,
                api_coordinator_broker_pids_unchanged=not all_roles, runtime_policy_unchanged=True,
                all_roles_replaced=all_roles, broker_stream_drained=all_roles, signed_hooks_delivered=20 if all_roles else 0,
                http_submissions=len(submissions), http_errors=failures, http_max_latency_ms=max(latencies),
                provider_calls=len(calls), stream_completed=True, one_answer_per_submission=True,
                sandbox='demo_mode', upstream='local_fixture', workflow_ids=['moyai-session-' + r for r in submissions])
    finally:
        traffic_stop.set()
        release.set()
        if traffic:
            traffic.join(timeout=12)
        if stream:
            stream.join(timeout=15)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        if output:
            output.mkdir(parents=True, exist_ok=True)
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            header = {'version': 2, 'width': 116, 'height': 20, 'timestamp': int(time.time()),
                      'title': 'Moyai compatible worker upgrade and rollback'}
            (output / 'worker-release.cast').write_text('\n'.join(json.dumps(e) for e in [header, *events]) + '\n')
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--all-roles', action='store_true', help='Rehearse broker, coordinator and API replacements after the worker handoff.')
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local PostgreSQL database.')
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as temporal:
        report = await asyncio.to_thread(probe, url, temporal.client.service_client.config.target_host, args.output, args.all_roles)
        try:
            for identity in report.pop('workflow_ids'):
                handle = temporal.client.get_workflow_handle(identity)
                await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
                await handle.terminate('Synthetic worker release probe complete')
        except BaseException:
            report.update(status='failed', failure_stage='workflow_replay')
            if args.output:
                (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            raise
        report['status'] = 'passed'
        report['workflow_histories_replayed'] = report['http_submissions']
        if args.output:
            (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
