"""Compare real MCP/GitHub code against the pre-fix revision using local HTTP fixtures.

Run: uv run python scripts/github_mcp_latency_demo.py [--serve]
The optional viewer is local-only at http://127.0.0.1:8897.
Each GitHub API request takes a controlled 100 ms; skills/memory each take 50 ms.
These are reproducible local timings, not a prediction of production latency.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
import time
from types import MethodType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.config import Settings
from app.main import create_app
from scripts.github_identity_fixture import IdentityProvider, repository_data

BASELINE = '5a7142a'
TOOLS = ['github_repositories', 'skills_search', 'memory_search']


class LocalSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **kwargs):
        return (init_settings,)  # Never load deployment credentials or a shared DB.


def baseline_file(path):
    return subprocess.check_output(['git', 'show', f'{BASELINE}:{path}'], cwd=ROOT, text=True)


def benchmark():
    original_client = httpx.AsyncClient
    for mode in ('before', 'after'):
        yield {'type': 'start', 'mode': mode}
        with TemporaryDirectory(prefix='moyai-latency-') as directory:
            app = create_app(LocalSettings(data_dir=directory, temporal_enabled=False,
                                          session_titles_enabled=False, auto_prepare_repositories=False))
            github = app.state.connectors.github
            github.save_app({'id': 123, 'slug': 'fixture', 'pem': 'fixture-key', 'owner_id': 44})
            selected = list(range(1000, 1015))
            app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 10,
                'account_id': 44, 'repository_ids': selected}, 'Local repositories')
            provider = IdentityProvider()
            provider.repos = {i: {**repository_data('BerriAI/litellm'), 'id': i,
                                  'full_name': f'Example/repo-{i}'} for i in selected}
            provider.installed = selected
            script = ROOT / 'agent/tools/mcp_bridge.py'
            if mode == 'before':
                # Execute the actual old refresh and bridge, not a timing model.
                namespace = {'__name__': 'app._baseline_repositories', '__package__': 'app'}
                exec(compile(baseline_file('app/github_repositories.py'), '<baseline>', 'exec'), namespace)
                github.refresh_connection = MethodType(namespace['GitHubRepositories'].refresh_connection, github)
                script = Path(directory) / 'mcp_before.py'
                source = baseline_file('sandbox/mcp_bridge.py')
                # Keep the historical bridge; resolve only its relocated dependencies.
                source = source.replace('import github_tools', 'from agent.tools import github_tools')
                source = source.replace('import credential_tools', 'from agent.tools import credential_tools')
                source = source.replace('import computer', 'from sandbox import computer')
                script.write_text(source)

            async def upstream(request):
                await asyncio.sleep(0.1)
                return provider.handle(request)

            class Broker(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    assert self.path == '/tools/call'
                    assert self.headers['Authorization'] == 'Bearer local-fixture'
                    if body['name'] == 'github_repositories':
                        data = asyncio.run(github.call({}, body['name'], {}))
                    else:
                        # Only the two unrelated broker responses are fixtures.
                        time.sleep(0.05)
                        data = {'matches': []}
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())

            server = ThreadingHTTPServer(('127.0.0.1', 0), Broker)
            thread = Thread(target=server.serve_forever, daemon=True)
            with patch.object(github, 'app_jwt', lambda config=None: 'fixture-jwt'), patch.object(
                    httpx, 'AsyncClient', lambda **kw: original_client(transport=httpx.MockTransport(upstream), **kw)):
                thread.start()
                process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT),
                         'WORKSPACE_BROKER_URL': f'http://127.0.0.1:{server.server_port}',
                         'WORKSPACE_RUN_TOKEN': 'local-fixture'})
                try:
                    process.stdin.write(json.dumps({'id': 'init', 'method': 'initialize'}) + '\n')
                    process.stdin.flush()
                    assert json.loads(process.stdout.readline())['id'] == 'init'
                    start = time.monotonic()
                    for name in TOOLS:
                        process.stdin.write(json.dumps({'id': name, 'method': 'tools/call',
                            'params': {'name': name, 'arguments': {}}}) + '\n')
                    process.stdin.close()
                    elapsed = {}
                    for _ in TOOLS:
                        reply = json.loads(process.stdout.readline())
                        assert not reply['result']['isError'], reply
                        data = json.loads(reply['result']['content'][0]['text'])
                        if reply['id'] == 'github_repositories':
                            assert [r['id'] for r in data['repositories']] == selected
                        elapsed[reply['id']] = round(time.monotonic() - start, 3)
                        yield {'type': 'result', 'mode': mode, 'tool': reply['id'],
                               'seconds': elapsed[reply['id']]}
                    assert process.wait(timeout=5) == 0
                    assert len(provider.calls) == (31 if mode == 'before' else 3)
                    if mode == 'after':
                        assert elapsed['skills_search'] < elapsed['github_repositories']
                        assert elapsed['memory_search'] < elapsed['github_repositories']
                    yield {'type': 'done', 'mode': mode, 'requests': len(provider.calls), 'repositories': 15}
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
    yield {'type': 'complete'}


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Moyai · GitHub and MCP latency</title>
<style>body{background:#f7f6fb;color:#241d36;font:16px/1.55 system-ui;margin:0}main{max-width:1040px;margin:48px auto;padding:0 28px}small{color:#6645bd;font-weight:700;letter-spacing:1px}h1{font-size:40px;line-height:1.15;margin:16px 0}p{color:#635c71}button{background:#5b3fd1;color:white;border:0;border-radius:9px;padding:13px 22px;font:600 16px system-ui;cursor:pointer}button:disabled{opacity:.55}table{width:100%;border-collapse:collapse;background:white;margin:24px 0;border-radius:12px;overflow:hidden}td,th{text-align:left;padding:15px 22px;border-bottom:1px solid #ece7f3}th{color:#6a587e;background:#f0ecf7}td:nth-child(3){color:#256343;font-weight:650}#status{background:#261d39;color:#edf5ee;padding:18px 24px;border-radius:10px;white-space:pre-line;min-height:50px}.note{font-size:14px}</style>
<main><small>MOYAI · LOCAL BACKEND VERIFICATION</small><h1>GitHub refresh stops holding<br>the other tools in line.</h1>
<p>Three requests arrive together through the real MCP bridge.<br>15 selected repositories · GitHub fixture: 100 ms per API request · Skills / memory fixture: 50 ms each</p>
<button id="run">Run before / after comparison</button>
<table><thead><tr><th>Response / work</th><th>Before fix</th><th>After fix</th></tr></thead><tbody>
<tr><td>github_repositories</td><td id="before-github_repositories">—</td><td id="after-github_repositories">—</td></tr>
<tr><td>skills_search</td><td id="before-skills_search">—</td><td id="after-skills_search">—</td></tr>
<tr><td>memory_search</td><td id="before-memory_search">—</td><td id="after-memory_search">—</td></tr>
<tr><td>GitHub API requests</td><td id="before-requests">—</td><td id="after-requests">—</td></tr></tbody></table>
<div id="status">Ready. Run the same workload against the prior revision and the current code.</div>
<p class="note">Actual measured local execution; controlled upstream delays, not production timings.<br>GitHub listings validate the same 15 IDs. Stateful tools remain ordered. Nothing is deployed.</p></main>
<script>document.querySelector('#run').onclick=()=>{const button=document.querySelector('#run');button.disabled=true;const status=document.querySelector('#status');const stream=new EventSource('/events');stream.onmessage=event=>{const data=JSON.parse(event.data);if(data.type==='start')status.textContent=(data.mode==='before'?'Before: serial bridge + per-repository verification.':'After: concurrent reads + scoped bulk metadata refresh.')+'\\nWaiting for actual responses…';if(data.type==='result')document.getElementById(data.mode+'-'+data.tool).textContent=data.seconds.toFixed(3)+' s';if(data.type==='done')document.getElementById(data.mode+'-requests').textContent=data.requests+' requests · '+data.repositories+' repositories';if(data.type==='complete'){status.textContent='PASS · GitHub API work: 31 → 3 requests. Same 15 selected repositories.\\nPASS · Skills and memory return while the GitHub refresh is still running.';stream.close();button.disabled=false;}if(data.type==='error'){status.textContent=data.message;stream.close();button.disabled=false;}};stream.onerror=()=>{status.textContent='Demo connection ended. Check the local server output.';stream.close();button.disabled=false;};};</script></html>'''


def serve():
    running = Lock()
    class Viewer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(PAGE.encode())
            elif self.path == '/events' and running.acquire(blocking=False):
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for row in benchmark():
                        self.wfile.write(('data: ' + json.dumps(row) + '\n\n').encode())
                        self.wfile.flush()
                finally:
                    running.release()
            else:
                self.send_error(404)
    server = ThreadingHTTPServer(('127.0.0.1', 8897), Viewer)
    print('Local demo: http://127.0.0.1:8897', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    if args.serve:
        serve()
    else:
        for event in benchmark():
            print(json.dumps(event), flush=True)
