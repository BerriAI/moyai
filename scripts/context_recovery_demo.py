"""Exercise real compaction requests and cold restores with a synthetic model server.

Run: python -m scripts.context_recovery_demo --checkpoints 30
No external writes or provider calls. This demonstrates recovery, not model quality.
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

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import digest
from sandbox.broker_transport import CONTENT_TYPE, seal
from sandbox.context_store import ContextStore, read_records
from sandbox.harness_agent import TurnJournal


def demonstrate(checkpoints, delay=0):
    requests = []
    final = 'Do not deploy. PR #127 already exists. Completed receipts remain in the journal. Verify release status next.'
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.startswith('/utils/token_counter'):
                raw = json.dumps({'total_tokens': len(json.dumps(payload['messages']).encode())}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            assert not {'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'tools'} & payload.keys()
            requests.append(payload)
            # Reproduce a complete summary that exceeds the saved byte budget.
            content = '界' * 5000 if len(requests) == 1 else final
            raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                              'usage': {'prompt_tokens': 100, 'completion_tokens': 3552}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    say = partial(print, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-recovery-') as temporary:
            root = Path(temporary)
            settings = Settings(_env_file=None, data_dir=root / 'app', public_url='http://127.0.0.1:8787',
                                litellm_api_key='synthetic-demo-key', litellm_api_base=f'http://127.0.0.1:{server.server_port}/v1',
                                modal_token_id='', modal_token_secret='', temporal_enabled=False,
                                session_titles_enabled=False)
            # Synthetic deployment limits, not claims about the live Astra model.
            from app.context_budget import ModelContextLimits
            settings.model_context_limits['openai/gpt-6-astra'] = ModelContextLimits(
                context_window=128000, max_input_tokens=128000, max_output_tokens=8000)
            app = create_app(settings)
            with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 50000)) as client:
                run = app.state.store.create_run('Verify release; do not deploy', '', 'modal', [], model='openai/gpt-6-astra')
                app.state.store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
                path = root / 'active.sqlite3'
                store = ContextStore(path, run['id'])
                store.initialize([{'role': 'user', 'content': final}])
                max_resume = 0
                say('Real broker + SQLite checkpoints; synthetic model replies, no provider calls.')
                say('First model reply: complete, 15,000 UTF-8 bytes; saved-summary budget: 12,000.')
                def summarize(previous, entries):
                    route = '/context/compact'
                    response = client.post(f"/broker/{run['id']}" + route,
                        content=seal('demo-capability', route, json.dumps({'summary': previous, 'entries': entries, 'cursor_protocol': 1}).encode()),
                        headers={'Authorization': 'Bearer demo-capability', 'Content-Type': CONTENT_TYPE})
                    response.raise_for_status()
                    return response.json()
                for checkpoint in range(checkpoints):
                    for index in range(12):
                        number = checkpoint * 12 + index
                        journal = TurnJournal([], f'Continue step {number}', store)
                        journal.tool_started(str(number), 'echo', {'number': number})
                        journal.tool_finished(str(number), f'receipt-{number}: ' + 'diagnostic result ' * 1500)
                    store.compact(summarize)
                    cursor = store.state()['cursor']
                    store.close()
                    cold = root / ('cold.sqlite3' if checkpoint % 2 == 0 else 'active.sqlite3')
                    shutil.copy2(path, cold)
                    path = cold
                    store = ContextStore(path, run['id'])
                    assert store.state()['cursor'] == cursor
                    history = store.history()[0]['content']
                    assert final in history
                    max_resume = max(max_resume, len(history.encode()))
                    if checkpoint == 0:
                        assert requests[0]['messages'][1] == requests[1]['messages'][1]
                        say('RECOVERED: regenerated from the original records; no summary slicing.')
                        say('PASS: no output-token cap injected; the complete summary is saved atomically.')
                    if checkpoint == 0 or (checkpoint + 1) % 10 == 0 or checkpoint == checkpoints - 1:
                        say(f'Cold checkpoint {checkpoint + 1}: {cursor} records summarized; resume input {len(history.encode()):,} bytes.')
                    if delay:
                        time.sleep(delay)
                records = store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
                assert records == 1 + checkpoints * 36
                assert 'receipt-0' in read_records(path, after=3, limit=1)[0]['text']
                rows = app.state.store.rows('SELECT status FROM model_requests WHERE run_id=?', (run['id'],))
                assert len(rows) == len(requests) and sum(r['status'] == 'failed' for r in rows) == 1
                event = next(e for e in app.state.store.events(run['id']) if e['kind'] == 'context')
                assert event['data']['reason'] == 'summary_too_large'
                say(f'PASS: {records:,} original records retained; first receipt retrievable; no task replay.')
                say(f'PASS: all {len(requests)} model attempts accounted; failure reason and byte count recorded.')
                store.close()
                return {'checkpoints': checkpoints, 'journal_records': records, 'maximum_resume_bytes': max_resume,
                        'model_requests': len(requests), 'recovery_reason': event['data']['reason'],
                        'inference': 'synthetic local model server; not live-provider validation'}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoints', type=int, default=30)
    parser.add_argument('--delay', type=float, default=0, help='Pause between real checkpoints for recording')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.checkpoints <= 100 or not 0 <= args.delay <= 2:
        parser.error('Use 1–100 checkpoints and delay 0–2')
    report = demonstrate(args.checkpoints, args.delay)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
