"""Real local broker/receiver requests; synthetic secrets, no provider or inference calls.

Run from an empty working directory with PYTHONPATH pointing at the repository:
  python /path/to/scripts/webhook_tools_demo.py
Then open http://127.0.0.1:8794/demo. The temporary DB is removed on exit.
"""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import uvicorn
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr

from app.config import Settings
from app.credentials import CredentialRequest, SaveSecret
from app.main import create_app
from app.security import digest

PAGE = '''<!doctype html><meta charset="utf-8"><title>Webhook receiver tools</title>
<style>body{font:18px system-ui;max-width:960px;margin:60px auto;background:#f5f5fa;color:#24243a}
button{padding:14px 22px;font:inherit;background:#5940cf;color:white;border:0;border-radius:8px}
li{padding:14px;margin:8px 0;background:white;border-radius:8px}p{line-height:1.6}</style>
<h1>Webhook receiver tools</h1><p>Real requests through Moyai's local broker and HTTP receiver.
Synthetic credentials only. No GitHub registration, scheduler, or inference worker.</p>
<button id="run">Run receiver verification</button><ol id="results"></ol>
<script src="/demo.js"></script>'''
JS = '''document.querySelector('#run').onclick=async()=>{
const b=document.querySelector('#run');b.disabled=true;const list=document.querySelector('#results');list.replaceChildren();
const response=await fetch('/demo/run',{method:'POST'});const data=await response.json();
for(const line of data.results){const li=document.createElement('li');li.textContent=line;list.append(li);await new Promise(r=>setTimeout(r,350));}
b.disabled=false;};'''


def demo(directory):
    values = {k: f.get_default(call_default_factory=True) for k, f in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8794', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    lock = asyncio.Lock()

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE

    @app.get('/demo.js')
    async def javascript():
        return Response(JS, media_type='text/javascript')

    @app.post('/demo/run')
    async def verify():
        async with lock:
            owner = app.state.store.identity({'method': 'local', 'role': 'admin'})
            run = app.state.store.create_run('Receiver verification', '', 'modal', [], chat_enabled=True, user_id=owner)
            app.state.store.claim_message(run['id'])
            app.state.store.update_run(run['id'], status='running', token_hash=digest('local-demo'))
            run = app.state.store.run(run['id'])
            # Required runtime placeholders; no scheduler/worker is started.
            settings = app.state.settings
            settings.temporal_enabled = True
            settings.modal_token_id = settings.modal_token_secret = 'synthetic-placeholder'
            settings.litellm_api_base = 'https://unused.invalid/v1'
            settings.litellm_api_key = 'synthetic-placeholder'
            results = []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=values['public_url']) as client:
                async def call(name, args, expected=200):
                    if name != 'automation_webhook_info':
                        args = {**args, 'turn_id': run['active_message_id']}
                    response = await client.post(f"/broker/{run['id']}/tools/call",
                        headers={'Authorization': 'Bearer local-demo'}, json={'name': name, 'arguments': args})
                    assert response.status_code == expected, (name, response.status_code)
                    return response.json()

                saved = await call('automation_create', {'request_key': 'demo-create', 'definition': {
                    'name': 'Receiver demo', 'prompt': 'Local demo only',
                    'event': {'provider': 'webhook', 'event': 'probe.ready'}}})
                target = {'automation_id': saved['id'], 'revision': 1}
                await call('automation_enable', {**target, 'request_key': 'before-config'}, 409)
                results.append('Before setup: enable rejected (HTTP 409), receiver not configured')
                secret = 'synthetic-local-webhook-secret'
                with app.state.store.connect() as conn:
                    secret_id = app.state.credentials.insert_secret(conn, SaveSecret(provider='generic',
                        name='webhook-signing-secret', label='Synthetic receiver demo', scope='personal', lifetime='persistent',
                        value=SecretStr(json.dumps({'WEBHOOK_SECRET': secret})), client_id=run['id']), owner, True)
                handle = app.state.credentials.request(run, CredentialRequest(provider='generic',
                    name='webhook-signing-secret', secret_id=secret_id, reason='Local receiver demo', request_key='demo-secret'))
                args = {**target, 'provider': 'webhook', 'credential_request_id': handle['request_id'], 'request_key': 'demo-setup'}
                configured = await call('automation_webhook_setup', args)
                assert configured['revision'] == 2 and configured['paused']
                assert secret not in json.dumps(configured)
                results.append('Setup: HTTP 200, revision 2, paused; no secret in response')
                retried = await call('automation_webhook_setup', args)
                assert retried['revision'] == 2
                results.append('Identical retry: revision remains 2, no secret rotation')
                enabled = await call('automation_enable', {**target, 'revision': 2, 'request_key': 'after-config'})
                assert not enabled['paused'] and enabled['status'] == 'scheduler_unavailable'
                results.append('Enable: receiver check passes; scheduler explicitly unavailable in this demo')
                path = '/hooks/automations/' + saved['id'] + '/webhook'
                response = await client.post(path, headers={'X-Webhook-Secret': secret}, json={'event': 'probe.ready'})
                assert response.status_code == 202 and response.json()['status'] == 'accepted'
                duplicate = await client.post(path, headers={'X-Webhook-Secret': secret}, json={'event': 'probe.ready'})
                assert duplicate.json()['status'] == 'duplicate'
                results.append('Real HTTP delivery: 202 accepted; repeated delivery deduplicated')
                await call('automation_pause', {**target, 'revision': 3, 'request_key': 'demo-pause'})
                info = await call('automation_webhook_info', {'automation_id': saved['id']})
                assert info['paused'] and info['provider_registration'] == 'not_verified'
                results.append('Inspection: receiver ready; remote registration unverified; demo paused')
                app.state.store.update_run(run['id'], status='idle')
            return {'results': results}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='webhook-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8794)
