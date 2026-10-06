"""Run real GitHub broker operations against an in-memory provider.

uv run python scripts/github_rulesets_demo.py              # CLI
uv run python scripts/github_rulesets_demo.py --serve      # http://127.0.0.1:8794
No live GitHub credentials or repository settings are used.
"""
import argparse
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.github import MANIFEST_PERMISSIONS
from app.main import create_app
from app.security import digest


def demonstrate(pause=0):
    def show(line):
        print(line, flush=True)
        time.sleep(pause)

    reviewers = [
        {'reviewer': {'id': 7, 'type': 'Team'}, 'file_patterns': ['*'], 'minimum_approvals': 1},
        {'reviewer': {'id': 7, 'type': 'Team'}, 'file_patterns': ['.github/**'], 'minimum_approvals': 1},
    ]
    ruleset = {'id': 42, 'name': 'guard-main', 'target': 'branch', 'source_type': 'Repository',
        'source': 'BerriAI/litellm', 'enforcement': 'active', 'updated_at': '2026-10-05T20:00:00Z',
        'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
        'bypass_actors': [{'actor_type': 'Team', 'actor_id': 99, 'bypass_mode': 'pull_request'}],
        'rules': [{'type': 'pull_request', 'parameters': {
            'required_approving_review_count': 1, 'require_code_owner_review': True,
            'required_reviewers': reviewers}},
            {'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'lint'}]}}]}
    original = copy.deepcopy(ruleset)
    scopes, writes = {}, []

    def provider(request):
        path = request.url.path
        if path == '/app/installations/10':
            return httpx.Response(200, json={'account': {'login': 'BerriAI', 'type': 'Organization'},
                'permissions': {**MANIFEST_PERMISSIONS, 'issues': 'write'}})
        if path.endswith('/access_tokens'):
            body = json.loads(request.content)
            assert body['repositories'] == ['litellm']
            token = f'demo-token-{len(scopes)}'
            scopes[token] = body['permissions']
            return httpx.Response(201, json={'token': token, 'expires_at': '2099-01-01T00:00:00Z'})
        if path == '/installation/repositories':
            return httpx.Response(200, json={'repositories': [{'full_name': 'BerriAI/litellm'}]})
        if path == '/repos/BerriAI/litellm/rulesets':
            return httpx.Response(200, json=[{key: ruleset[key] for key in ('id', 'name', 'source', 'source_type')}])
        assert path == '/repos/BerriAI/litellm/rulesets/42'
        permission = scopes[request.headers['authorization'].removeprefix('Bearer ')]
        if request.method == 'PUT':
            assert permission == {'administration': 'write'}
            body = json.loads(request.content)
            assert list(body) == ['rules']
            writes.append(body)
            ruleset.update(rules=body['rules'], updated_at='2026-10-05T20:01:00Z')
        result = copy.deepcopy(ruleset)
        if 'administration' not in permission:
            result.pop('bypass_actors')
        return httpx.Response(200, json=result)

    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    with TemporaryDirectory(prefix='moyai-ruleset-demo-') as directory:
        values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8794')
        app = create_app(Settings(_env_file=None, **values))
        with TestClient(app, base_url=values['public_url'], client=('127.0.0.1', 50000)) as client:
            github = app.state.connectors.github
            github.save_app({'id': 123, 'slug': 'moyai-demo', 'pem': 'demo-only'})
            credentials = {'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/litellm'}
            app.state.connectors.save('github', credentials, 'Simulated GitHub')
            run = app.state.store.create_run('Remove only the wildcard reviewer requirement', '', 'modal', ['github'])
            app.state.store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
            def call(name, arguments=None):
                result = client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer demo-capability'},
                                     json={'name': name, 'arguments': arguments or {}})
                assert result.status_code == 200, result.text
                return result.json()
            http_client = httpx.AsyncClient
            with patch('app.github.httpx.AsyncClient', lambda **kw: http_client(transport=httpx.MockTransport(provider), **kw)), \
                    patch.object(github, 'app_jwt', return_value='demo-jwt'):
                show('LOCAL DEMO | Real Moyai broker + HTTP client | Simulated GitHub')
                show('BEFORE: Engineering required on * and .github/**')
                assert asyncio.run(github.verify(credentials)) == 'BerriAI/litellm'
                show('PASS  Connect an App with Administration + extra Issues permission')
                listing = call('github_rulesets')
                assert listing['rulesets'][0]['id'] == 42
                show('POST  github_rulesets -> guard-main (repository branch ruleset)')
                read = call('github_ruleset', {'ruleset_id': 42})
                assert len(read['ruleset']['rules'][0]['parameters']['required_reviewers']) == 2
                show('POST  github_ruleset -> * and .github/**; token scope: Metadata read')
                args = {'repository': 'BerriAI/litellm', 'ruleset_id': 42,
                        'revision': read['revision'], 'required_reviewers': reviewers[1:]}
                show('POST  github_update_ruleset_reviewers -> remove only the * entry')
                result = call('github_update_ruleset_reviewers', args)
                assert result['changed'] is True, result
                show('PASS  Updated and read back: .github/** reviewer requirement remains')
                expected = copy.deepcopy(original)
                expected['rules'][0]['parameters']['required_reviewers'] = reviewers[1:]
                expected['updated_at'] = ruleset['updated_at']
                assert expected == ruleset
                show('PASS  1 approval + code-owner review + lint + branch scope preserved')
                show('PASS  Bypass actors preserved; update token: Administration write only')
                stale = call('github_update_ruleset_reviewers', args)
                assert 'changed since it was read' in stale['error'] and len(writes) == 1
                show('PASS  Stale revision rejected; exactly one GitHub update sent')
                show('VERIFIED | No live GitHub settings were changed')
            app.state.store.update_run(run['id'], status='completed', token_hash='')


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai ruleset demo</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f3f8;color:#252136;font:17px system-ui}main{max-width:1140px;margin:48px auto;padding:0 28px}
small{color:#6846c7;font-weight:650;letter-spacing:.08em}h1{font-size:38px;margin:12px 0}p{color:#646073;line-height:1.6;margin:10px 0 24px}
button{background:#6541c7;border:0;border-radius:10px;color:white;padding:13px 22px;font:600 16px system-ui;cursor:pointer}button:disabled{opacity:.55}
pre{background:#191827;color:#eeeafc;border-radius:16px;padding:26px;min-height:430px;font:15px/2 ui-monospace,monospace;white-space:pre-wrap;margin-top:24px;box-shadow:0 8px 30px #22143314}
</style><main><small>MOYAI DEVIN · LOCAL BROKER DEMO</small><h1>Fix reviewer noise. Keep the other rules.</h1>
<p>Actual requests through Moyai’s broker and GitHub client, using a simulated GitHub provider.<br>No live repository settings or credentials are used.</p>
<button id="run">Run reviewer update</button><pre id="log">Ready. Start the demo to inspect guard-main and remove its wildcard reviewer entry.</pre>
<script>
document.querySelector('#run').onclick=async()=>{const button=document.querySelector('#run'),log=document.querySelector('#log');button.disabled=true;log.textContent='';
try{const response=await fetch('/run',{method:'POST'});if(!response.ok)throw Error('Demo failed');const reader=response.body.getReader(),decoder=new TextDecoder();
while(true){const {value,done}=await reader.read();if(done)break;log.textContent+=decoder.decode(value,{stream:true});}}
catch(e){log.textContent+='ERROR: '+e.message}finally{button.disabled=false;}};
</script></main>'''


def serve():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(PAGE.encode())

        def do_POST(self):
            if self.path != '/run':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            with subprocess.Popen([sys.executable, __file__, '--pause', '1.5'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                for line in process.stdout:
                    if b'StarletteDeprecationWarning' in line or b'from starlette.testclient' in line:
                        continue
                    self.wfile.write(line)
                    self.wfile.flush()
                process.wait()
    print('Local demo: http://127.0.0.1:8794', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8794), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--pause', type=float, default=0)
    args = parser.parse_args()
    serve() if args.serve else demonstrate(args.pause)
