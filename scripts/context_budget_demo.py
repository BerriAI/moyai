"""Real broker, HTTP counter, journal and handoffs; synthetic model/task loop.

Run: python -m scripts.context_budget_demo --steps 30 --delay .5
No external services, production changes or provider generation calls.
"""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import digest
from sandbox.broker_transport import CONTENT_TYPE, seal
from sandbox.context_recovery import run_with_context_recovery
from sandbox.context_store import ContextStore, read_records
from sandbox.harness_agent import TurnJournal


def demonstrate(steps=30, delay=0):
    large, small = 'openai/gpt-6-astra', 'fireworks_ai/glm-5p3'
    windows = {large: 64000, small: 40000}  # Deliberately small synthetic deployment limits.
    requests, actions, blocked, summaries = [], [], [], []
    say = partial(print, flush=True)

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            assert self.path.startswith('/model/info')
            self.reply({'data': [{'model_name': model, 'model_info': {
                'context_window': window, 'max_input_tokens': window, 'max_output_tokens': 12000}}
                for model, window in windows.items()]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.startswith('/utils/token_counter'):
                return self.reply({'total_tokens': len(body['messages'][0]['content'][0]['text'].encode()),
                                   'tokenizer_type': 'synthetic_bytes'})
            requests.append(body)
            if self.path.endswith('/chat/completions'):
                assert not {'max_tokens', 'max_completion_tokens', 'max_output_tokens'} & body.keys()
                summaries.append(body)
                return self.reply({'choices': [{'finish_reason': 'stop', 'message': {'content':
                    f'Do not deploy. Receipts 0 through {len(actions) - 1} are saved in the original journal. '
                    f'Next unfinished step is {len(actions)}. Do not repeat completed writes.'}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})
            assert body['max_output_tokens'] == 8000
            self.reply({'status': 'completed', 'output': [], 'usage': {'input_tokens': 100, 'output_tokens': 10}})

    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-budget-demo-') as directory:
            root = Path(directory)
            settings = Settings(_env_file=None, data_dir=root / 'app', public_url='http://127.0.0.1:8787',
                litellm_api_key='synthetic-demo-key', litellm_api_base=f'http://127.0.0.1:{gateway.server_port}/v1',
                session_titles_enabled=False, modal_token_id='', modal_token_secret='', temporal_enabled=False)
            app = create_app(settings)
            with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 50000)) as client:
                run = app.state.store.create_run('Do not deploy. Create numbered local test receipts.', '', 'modal', [], model=large)
                app.state.store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
                def post(route, body):
                    return client.post(f"/broker/{run['id']}" + route,
                        content=seal('demo-capability', route, json.dumps(body).encode()),
                        headers={'Authorization': 'Bearer demo-capability', 'Content-Type': CONTENT_TYPE})
                def compact(previous, entries, **kwargs):
                    response = post('/context/compact', {'summary': previous, 'entries': entries,
                                                        'cursor_protocol': 1, **kwargs})
                    response.raise_for_status()
                    return response.json()
                store = ContextStore(root / 'context.sqlite3', run['id'])
                store.initialize([])
                relay = SimpleNamespace(context_required=None, compact=compact)
                resumes = []
                def commentary(text):
                    resumes.append(1)
                    if len(resumes) in {1, 5, 10}:
                        say(f'COMPACT + RESUME {len(resumes)}: receipts saved; original task continues.')
                agent = SimpleNamespace(context=SimpleNamespace(relay=relay, spec={}, cwd=str(root),
                    activity=SimpleNamespace(commentary=commentary)), context_store=store,
                    journal=TurnJournal([], run['prompt'], store), stopped=threading.Event(), pending_text=[])
                say('Real broker, HTTP token counter, SQLite and context handoff; synthetic inference/task loop.')
                say('Start with a 64,000-token test window; preserve the requested 8,000 output tokens.')
                def invoke(prompt):
                    history = [{'role': 'user', 'content': prompt}]
                    while True:
                        response = post('/v1/responses', {'input': history, 'max_output_tokens': 8000})
                        if response.status_code == 409:
                            pressure = response.json()['detail']
                            assert response.headers['X-Moyai-Context'] == 'compact'
                            relay.context_required = pressure
                            blocked.append(pressure)
                            if len(blocked) == 1:
                                say(f"BLOCKED BEFORE INFERENCE: input {pressure['input_tokens']:,} > budget {pressure['input_budget']:,}.")
                            return {'failed': True}
                        response.raise_for_status()
                        if len(actions) == steps:
                            agent.journal.finish('Completed all local test receipts.')
                            return {'completed': True}
                        index = len(actions)
                        agent.journal.tool_started(str(index), 'fixture_write', {'number': index})
                        receipt = root / f'receipt-{index}.txt'
                        assert not receipt.exists(), 'Repeated task action'
                        receipt.write_text(f'Completed receipt {index}')
                        actions.append(index)
                        output = f'Receipt {index} saved. ' + 'diagnostic output ' * 1600
                        agent.journal.tool_finished(str(index), output)
                        history.append({'role': 'user', 'content': output})
                        if len(actions) == steps // 2:
                            app.state.store.execute('UPDATE runs SET active_model=? WHERE id=?', (small, run['id']))
                            say('MODEL CHANGE: switch to a 40,000-token test window; next request uses the new budget.')
                        if delay:
                            time.sleep(delay)
                result = run_with_context_recovery(agent, run['prompt'], [], invoke)
                assert result['completed'] and actions == list(range(steps))
                assert any(p['context_window'] == windows[small] for p in blocked)
                records = store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
                store.close()
                cold = root / 'cold.sqlite3'
                shutil.copy2(root / 'context.sqlite3', cold)
                restored = ContextStore(cold, run['id'])
                assert 'Receipt 0 saved' in read_records(cold, after=2, limit=1)[0]['text']
                assert not restored.pending
                restored.close()
                rows = app.state.store.rows('SELECT status FROM model_requests WHERE run_id=?', (run['id'],))
                assert len(rows) == len(requests) and all(row['status'] == 'completed' for row in rows)
                say(f'PASS: {steps} local writes, each executed once; {len(resumes)} automatic context handoffs.')
                say(f'PASS: {records} journal records survive a cold restore; first receipt remains retrievable.')
                say(f'PASS: {len(blocked)} oversized requests never reached inference; all {len(requests)} admitted calls accounted.')
                say('PASS: output allowance unchanged; summaries complete; production untouched.')
                return {'steps': steps, 'handoffs': len(resumes), 'blocked_requests': len(blocked),
                        'admitted_requests': len(requests), 'summary_requests': len(summaries), 'journal_records': records,
                        'duplicate_actions': 0, 'output_allowance': 8000,
                        'inference': 'synthetic model and task loop; real broker/counter HTTP, storage and handoffs'}
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steps', type=int, default=30)
    parser.add_argument('--delay', type=float, default=0)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 4 <= args.steps <= 100 or not 0 <= args.delay <= 2:
        parser.error('Use 4–100 steps and delay 0–2')
    report = demonstrate(args.steps, args.delay)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
