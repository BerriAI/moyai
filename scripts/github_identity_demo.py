"""Local ID migration demo. Run: uv run python scripts/github_identity_demo.py

Serves the actual Moyai app at http://127.0.0.1:8796 and a walkthrough at /demo.
GitHub HTTP responses use a local fixture. No live credentials or writes.
"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from app.config import Settings
from app.main import create_app
from app.security import digest
from scripts.github_identity_fixture import IdentityProvider

PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai · Repository rename demo</title>
<link rel="stylesheet" href="/demo/style.css"><main>
<span class="eyebrow">MOYAI · LOCAL VERIFICATION</span><h1>Rename the repository.<br>Keep the connection.</h1>
<p>This is Moyai’s real broker and GitHub client, with a local GitHub fixture.<br>No live GitHub repository is renamed or modified.</p>
<section><div><small>GitHub name</small><strong id="name">BerriAI/moyai-devin</strong></div><div><small>Permanent repository ID</small><strong>202</strong></div></section>
<div class="actions"><button id="connect">1 · Read the original repository</button><button id="rename" disabled>2 · Rename and read again</button><button id="reject" disabled>3 · Check name reuse</button></div>
<pre id="result">Ready. The saved connection still contains the old name.</pre>
<a href="/#connections" target="_blank">Open the actual Moyai repository picker ↗</a>
</main><script src="/demo/script.js"></script>'''
CSS = '''body{margin:0;background:#f5f3fa;color:#272334;font-family:system-ui}main{max-width:1000px;margin:52px auto;padding:0 32px}.eyebrow{color:#6948b5;font-size:13px;font-weight:700;letter-spacing:.12em}h1{font-size:44px;line-height:1.1;letter-spacing:-1.5px;margin:20px 0}p{color:#62596e;line-height:1.65}section{display:flex;gap:80px;padding:24px 28px;background:white;border:1px solid #e2dbea;border-radius:14px;margin:26px 0}small{display:block;color:#756b80;margin-bottom:8px}strong{font-size:22px}.actions{display:flex;gap:12px}button{font:600 14px system-ui;color:white;background:#6541c7;border:0;border-radius:9px;padding:13px 17px;cursor:pointer}button:disabled{opacity:.4;cursor:default}pre{background:#201a2c;color:#d7f7dc;font:15px/1.75 ui-monospace,monospace;padding:24px;border-radius:14px;white-space:pre-wrap;min-height:150px}a{color:#6541c7;font-size:14px}'''
JS = '''let csrf,run,token;
async function api(path,body,capability=false){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json',...(capability?{Authorization:'Bearer '+token}:{'X-CSRF-Token':csrf})},...(body?{body:JSON.stringify(body)}:{})});const data=await r.json();if(!r.ok)throw Error(data.detail||'Request failed');return data;}
async function initialize(){const session=await api('/api/session');csrf=session.csrf;const data=await api('/demo/session',{});run=data.run;token=data.token;}const ready=initialize();
async function read(id){return api('/broker/'+run+'/tools/call',{name:'github_checkout',arguments:id?{repository_id:id}:{}},true);}
function show(message){document.querySelector('#result').textContent=message;}
document.querySelector('#connect').onclick=async()=>{try{await ready;const r=await read();if(r.error)throw Error(r.error);show('PASS · Legacy selection migrated to permanent ID '+r.repository_id+'\\nRepository: '+r.repository+'\\nCheckout route: '+r.git_path+'\\nOnly this selected repository is authorized.');document.querySelector('#rename').disabled=false;}catch(e){show('ERROR · '+e.message);}};
document.querySelector('#rename').onclick=async()=>{try{await api('/demo/rename',{});const r=await read();if(r.error)throw Error(r.error);document.querySelector('#name').textContent=r.repository;show('PASS · '+r.repository+' is readable after rename\\nPermanent ID: '+r.repository_id+' (unchanged)\\nCheckout route: '+r.git_path+' (unchanged)\\nNo reconnect, environment edit, or deployment required.');document.querySelector('#reject').disabled=false;}catch(e){show('ERROR · '+e.message);}};
document.querySelector('#reject').onclick=async()=>{try{const r=await read(999);if(!r.error)throw Error('Unexpected access');show('PASS · Renamed repository remains ID 202\\nPASS · Replacement repository ID 999 is blocked\\n'+r.error+'\\nSaved selection remains [202].');}catch(e){show('ERROR · '+e.message);}};'''


def main():
    with TemporaryDirectory(prefix='moyai-identity-demo-') as directory:
        settings = Settings(_env_file=None, data_dir=directory, public_url='http://127.0.0.1:8796',
                            litellm_api_key='', modal_token_id='', modal_token_secret='', temporal_enabled=False,
                            auto_prepare_repositories=False, session_titles_enabled=False,
                            litellm_trace_api_key='', langfuse_secret_key='', langsmith_api_key='', braintrust_api_key='', raindrop_write_key='')
        app = create_app(settings)
        github, store = app.state.connectors.github, app.state.store
        github.save_app({'id': 123, 'slug': 'local-fixture', 'pem': 'fixture-key', 'owner_id': 44})
        app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/moyai-devin'}, 'BerriAI/moyai-devin')
        provider = IdentityProvider()
        provider.repos[202]['full_name'] = 'BerriAI/moyai-devin'
        provider.repos[202]['html_url'] = 'https://github.com/BerriAI/moyai-devin'
        demo_run = None

        @app.get('/demo', response_class=HTMLResponse)
        async def page():
            return PAGE

        @app.get('/demo/style.css')
        async def style():
            return Response(CSS, media_type='text/css')

        @app.get('/demo/script.js')
        async def script():
            return Response(JS, media_type='application/javascript')

        @app.post('/demo/session')
        async def session(request: Request):
            nonlocal demo_run
            app.state.security.require(request, mutation=True, admin=True)
            if demo_run is None:
                demo_run = store.create_run('Verify a repository rename', 'https://github.com/BerriAI/moyai-devin', 'modal', ['github'])['id']
            store.update_run(demo_run, status='running', token_hash=digest('local-demo-capability'))
            return {'run': demo_run, 'token': 'local-demo-capability'}

        @app.post('/demo/rename')
        async def rename(request: Request):
            app.state.security.require(request, mutation=True, admin=True)
            provider.repos[202]['full_name'] = 'BerriAI/moyai'
            provider.repos[202]['html_url'] = 'https://github.com/BerriAI/moyai'
            provider.repos[999] = {**provider.repos[202], 'id': 999, 'full_name': 'BerriAI/moyai-devin'}
            if 999 not in provider.installed:
                provider.installed.append(999)
            return {'renamed': True}

        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(provider.handle), **kwargs)
        with patch.object(github, 'app_jwt', lambda config=None: 'fixture-jwt'), patch.object(httpx, 'AsyncClient', client):
            uvicorn.run(app, host='127.0.0.1', port=8796, log_level='warning')


if __name__ == '__main__':
    main()
