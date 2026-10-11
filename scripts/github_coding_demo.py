"""Local demonstration: real broker/packager, simulated GitHub; no live writes.

uv run --frozen python -m scripts.github_coding_demo
Open http://127.0.0.1:8797/demo .
"""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from agent.tools import github_tools
from app.config import Settings
from app.main import create_app
from app.security import digest
from scripts.github_coding_fixture import CodingProvider, PR, REPOSITORY, COMMIT

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Moyai GitHub coding tools</title>
<style>
body{background:#f6f5fa;color:#24202f;font:16px/1.55 system-ui;margin:0}main{max-width:1000px;margin:42px auto;padding:0 30px}.eyebrow{font-size:13px;letter-spacing:.14em;color:#6850ac;font-weight:700}h1{font-size:40px;letter-spacing:-1.2px;margin:10px 0}p{color:#655d71}button{font:600 15px system-ui;background:#6142b7;color:white;border:0;padding:13px 18px;border-radius:9px;cursor:pointer}button:disabled{opacity:.5}.cards{display:flex;gap:16px;margin:24px 0}.card{background:white;border:1px solid #e3deed;border-radius:12px;padding:16px 22px;flex:1}.card strong{display:block;font-size:28px}.card small{color:#6a6077}pre{background:#211b2c;color:#d9f1dd;padding:22px;border-radius:12px;font:14px/1.7 ui-monospace,monospace;white-space:pre-wrap;min-height:265px}.note{font-size:13px}.actions{display:flex;gap:10px;flex-wrap:wrap}</style>
<main><div class="eyebrow">MOYAI · GITHUB CONNECTOR VERIFICATION</div><h1>Complete the coding workflow.</h1>
<p>Actual Moyai broker, permissions and local Git packager. GitHub responses are simulated.<br>No live repository writes or production deployment.</p>
<div class="cards"><div class="card"><strong>31</strong><small>new coding tools</small></div><div class="card"><strong>.env.example</strong><small>accepted in both publishing layers</small></div><div class="card"><strong>No merge / approval</strong><small>comment-only reviews</small></div></div>
<div class="actions"><button id="run">Run the coding workflow</button><button id="audit">Run trace-audit additions</button><button id="bounds">Verify the boundaries</button></div>
<pre id="result">Ready. Run actual requests and inspect the results here.</pre>
<p class="note">Fixture scope: one selected repository, one confirmed workspace PR, current head and workflow attempt.</p></main>
<script>
let csrf;const out=document.querySelector('#result');
function line(s){out.textContent+=s+'\\n'}
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.detail||JSON.stringify(data));return data}
async function init(){csrf=(await(await fetch('/api/session')).json()).csrf}const ready=init();
function pause(){return new Promise(resolve=>setTimeout(resolve,900))}
async function step(label,operation){const data=await post('/demo/step',{operation});line(label+' · '+data.message);await pause()}
document.querySelector('#run').onclick=async function(){this.disabled=true;out.textContent='';try{await ready;await step('PASS','template');await step('PASS','metadata');await step('PASS','review');await step('PASS','workflow');await step('PASS','receipt');line('Workflow complete. All calls ran through the real broker.')}catch(error){line('ERROR · '+error.message)}finally{this.disabled=false}};
document.querySelector('#audit').onclick=async function(){this.disabled=true;out.textContent='';try{await ready;await step('PASS','capabilities');await step('PASS','publication');await step('PASS','close');line('Audit additions complete. No merge or approval capability.')}catch(error){line('ERROR · '+error.message)}finally{this.disabled=false}};
document.querySelector('#bounds').onclick=async function(){this.disabled=true;out.textContent='';try{await ready;await step('PASS','secret');await step('PASS','merge');await step('PASS','approve');await step('PASS','head');line('No forbidden operation reached the provider.')}catch(error){line('ERROR · '+error.message)}finally{this.disabled=false}};
</script></html>'''


CSS = PAGE.split('<style>', 1)[1].split('</style>', 1)[0]
JS = PAGE.split('<script>', 1)[1].split('</script>', 1)[0]
PAGE = PAGE.replace('<style>' + CSS + '</style>', '<link rel="stylesheet" href="/demo/style.css">').replace(
    '<script>' + JS + '</script>', '<script src="/demo/script.js"></script>')


def main():
    with TemporaryDirectory(prefix='moyai-coding-demo-') as directory:
        settings = Settings(_env_file=None, data_dir=directory, public_url='http://127.0.0.1:8797',
            agent_harness='hermes', litellm_api_key='', modal_token_id='', modal_token_secret='',
            temporal_enabled=False, auto_prepare_repositories=False, session_titles_enabled=False,
            repository_context_enabled=False, bundled_skills_enabled=False, litellm_trace_api_key='',
            langfuse_secret_key='', langsmith_api_key='', braintrust_api_key='', raindrop_write_key='')
        app = create_app(settings)
        store, github = app.state.store, app.state.connectors.github
        github.save_app({'id': 123, 'slug': 'coding-fixture', 'pem': 'fixture-key', 'owner_id': 44})
        credentials = {'kind': 'github_app', 'installation_id': 10, 'account_id': 44, 'repository_ids': [101]}
        github.remember_repository(REPOSITORY, credentials)
        app.state.connectors.save('github', credentials, 'Local simulated GitHub')
        provider = CodingProvider()
        run = store.create_run('GitHub coding workflow demo', '', 'modal', ['github'])
        store.update_run(run['id'], status='running', token_hash=digest('local-demo-capability'))
        store.execute('''INSERT INTO github_publications
            (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at) VALUES(?,?,?,?,?,?,?,?)''',
            ('demo-publication', run['id'], 0, 'fixture', PR['head']['ref'], json.dumps({'number': 100, 'repository_id': 101}), github.connection_version(), 'fixture'))
        workspace = Path(directory) / 'workspace'
        repo = workspace / 'repo'
        repo.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(['git', '-c', 'user.name=Demo', '-c', 'user.email=demo@example.invalid', *args], cwd=repo).decode().strip()
        git('init', '-q', '-b', 'main')
        (repo / '.env.example').write_text('AGENT_HARNESS=pydantic-ai\n')
        git('add', '.env.example');git('commit', '-qm', 'Fixture base')
        (repo / '.git/moyai.json').write_text(json.dumps({'repository_id': 101, 'repository': REPOSITORY['full_name'], 'base_sha': git('rev-parse', 'HEAD')}))
        (repo / '.env.example').write_text('AGENT_HARNESS=deepagents-temporal\n')
        args = {'repository_id': 101, 'number': 100, 'head_sha': COMMIT}
        counter = 0

        @app.get('/demo', response_class=HTMLResponse)
        async def demo():
            return PAGE

        @app.get('/demo/style.css')
        async def style():
            return Response(CSS, media_type='text/css')

        @app.get('/demo/script.js')
        async def script():
            return Response(JS, media_type='text/javascript')

        @app.post('/demo/step')
        async def step(request: Request):
            nonlocal counter
            app.state.security.require(request, mutation=True, admin=True)
            store.update_run(run['id'], status='running', token_hash=digest('local-demo-capability'))
            operation = (await request.json())['operation']
            counter += 1
            key = f'demo-action-{counter}'
            async def invoke(name, values):
                # ASGI transport executes the same authenticated broker route as the agent.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_url) as client:
                    response = await client.post(f'/broker/{run["id"]}/tools/call', headers={'Authorization': 'Bearer local-demo-capability'},
                                                 json={'name': name, 'arguments': values})
                return response, response.json()
            if operation == 'template':
                from app.github import Publish
                with patch.object(github_tools, 'ROOT', workspace):
                    payload = github_tools.collect(str(repo), 'Update harness', 'Template only', key)
                parsed = Publish.model_validate(payload)
                assert parsed.files[0].path == '.env.example'
                return {'message': 'Real Git diff packaged; broker accepts .env.example → deepagents-temporal'}
            if operation == 'secret':
                from app.github import Change
                from pydantic import ValidationError
                try:
                    Change(path='.env.production', content='EXAMPLE=private')
                except ValidationError:
                    return {'message': '.env.production remains blocked before publication'}
                raise AssertionError('Secret path accepted')
            if operation == 'capabilities':
                _, result = await invoke('github_capabilities', {'repository_id': 101})
                assert not any(t['missing_permissions'] for t in result['tools'])
                return {'message': 'Registered tools and installed permissions checked; merge/approval prohibited'}
            if operation == 'publication':
                import hashlib
                identity = hashlib.sha256(f'{run["id"]}:demo-publication-status'.encode()).hexdigest()
                receipt = {'number': 100, 'repository_id': 101, 'repository': REPOSITORY['full_name']}
                store.execute('''INSERT INTO github_publications
                    (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
                    VALUES(?,?,0,'fixture',?,?,?,'fixture') ON CONFLICT DO NOTHING''',
                    (identity, run['id'], PR['head']['ref'], json.dumps(receipt), github.connection_version()))
                before = len(provider.dispatches)
                _, result = await invoke('github_publication_status', {'repository_id': 101, 'request_key': 'demo-publication-status'})
                assert result['status'] == 'confirmed_receipt' and len(provider.dispatches) == before
                return {'message': 'Publication receipt found; zero GitHub mutation requests'}
            if operation in {'merge', 'approve'}:
                name = 'github_merge_pull_request' if operation == 'merge' else 'github_comment_review'
                response, result = await invoke(name, {**args, 'request_key': key, **({'body': 'Review', 'event': 'APPROVE'} if operation == 'approve' else {})})
                assert response.status_code != 200 or 'error' in result
                return {'message': 'Merge tool is unavailable' if operation == 'merge' else 'APPROVE argument rejected; review event is fixed to COMMENT'}
            if operation == 'head':
                _, result = await invoke('github_update_pull_request_details', {**args, 'head_sha': 'a'*40, 'title': 'Stale', 'request_key': key})
                assert 'error' in result
                return {'message': 'Stale PR head rejected before dispatch'}
            name, values, message = {
                'close': ('github_close_pull_request', {}, 'Authorized PR closed with state=closed; no merge or branch deletion'),
                'metadata': ('github_update_pull_request_details', {'title': 'Deep Agents + Temporal'}, 'PR title updated at the verified head'),
                'review': ('github_comment_review', {'body': 'Verified the template migration'}, 'Review submitted with COMMENT; no approval'),
                'workflow': ('github_workflow_action', {'run_id': 7, 'run_attempt': 2, 'action': 'rerun_failed'}, 'Failed CI jobs rerun only for this PR, head and attempt'),
                'receipt': ('github_update_pull_request_details', {'body': 'Verified coding workflow'}, 'Repeated request returns saved receipt; exactly one provider write'),
            }[operation]
            before = len(provider.dispatches)
            response, result = await invoke(name, {**args, **values, 'request_key': key})
            if not result.get('confirmed'):
                raise HTTPException(500, str(result))
            if operation == 'receipt':
                _, repeated = await invoke(name, {**args, **values, 'request_key': key})
                assert repeated == result and len(provider.dispatches) == before + 1
            return {'message': message}

        original = httpx.AsyncClient
        def http_client(**kwargs):
            if 'transport' not in kwargs:
                kwargs['transport'] = httpx.MockTransport(provider.handle)
            return original(**kwargs)
        with patch('app.github.httpx.AsyncClient', http_client), patch.object(github, 'app_jwt', return_value='fixture-jwt'):
            uvicorn.run(app, host='127.0.0.1', port=8797)


if __name__ == '__main__':
    main()
