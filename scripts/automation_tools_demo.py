"""Local backend demo: real broker requests and a real disposable Temporal server.

Run `uv run python scripts/automation_tools_demo.py`, then open
http://127.0.0.1:8793/demo. No provider credentials or inference are used.
Temporal's official development server may be downloaded on the first run.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from temporalio.testing import WorkflowEnvironment

from app.config import Settings
from app.main import create_app
from app.security import digest


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai automation tools · local demo</title>
<style>
body{font:18px system-ui;color:#262336;background:#f7f6fb;margin:0;padding:40px}main{max-width:1160px;margin:auto}
h1{font-size:36px;margin:10px 0}p{color:#625d72;line-height:1.5}button{background:#5b3fd1;color:white;border:0;border-radius:9px;padding:14px 22px;font:inherit;cursor:pointer}button:disabled{opacity:.5}
.badge{font-size:14px;letter-spacing:.08em;color:#5b3fd1;font-weight:700}#state{float:right;background:white;padding:20px;border-radius:12px;max-width:430px;min-height:50px;white-space:pre-line;font-size:17px}#steps{margin-top:30px}
.step{padding:14px 20px;background:white;border:1px solid #e3dfee;border-radius:10px;margin:10px 0;display:grid;grid-template-columns:290px 1fr;gap:24px}.step strong{font-size:18px}.step code{display:block;color:#625d72;font-size:14px;margin-top:6px}.result{font-size:17px;white-space:pre-wrap;color:#215c45}footer{font-size:14px;color:#6b657b;margin-top:24px}
</style><main><div class="badge">MOYAI DEVIN · LOCAL BACKEND DEMO</div>
<h1>Manage scheduled work from chat</h1><p>Actual agent-tool requests through Moyai’s broker, with a real local Temporal scheduler.<br>No model calls, repository changes, or production schedules.</p>
<div id="state">Ready to run</div><button id="run">Run real tools demo</button><div id="steps"></div>
<footer>This page displays real request results. It is a demonstration harness, not a new product screen.<br>The local scheduler has no inference worker. The final step pauses the demonstration schedule.</footer></main>
<script>
const names=['List existing automations','Create weekly audit','Enable and confirm next run','Update the cadence','Enable the updated schedule','Pause future runs'];
document.querySelector('#run').onclick=async()=>{document.querySelector('#run').disabled=true;document.querySelector('#steps').replaceChildren();
for(let i=0;i<6;i++){const row=document.createElement('div');row.className='step';const label=document.createElement('div');const title=document.createElement('strong');title.textContent=names[i];label.append(title);const code=document.createElement('code');label.append(code);const out=document.createElement('div');out.className='result';out.textContent='Sending broker request…';row.append(label,out);document.querySelector('#steps').append(row);
const response=await fetch('/demo/step/'+i,{method:'POST'});const data=await response.json();if(!response.ok){out.textContent=JSON.stringify(data);break}code.textContent=data.tool+' → HTTP '+data.http_status;out.textContent=data.summary;document.querySelector('#state').textContent=data.state;await new Promise(resolve=>setTimeout(resolve,3200));}
document.querySelector('#run').disabled=false;};
</script>'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8793', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    original_lifespan = app.router.lifespan_context
    context = {}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
            async with original_lifespan(app):
                # Fake runtime placeholders satisfy readiness checks. No worker or
                # model is started; only the real scheduler and broker are exercised.
                settings = app.state.settings
                settings.temporal_enabled = True
                settings.modal_token_id = settings.modal_token_secret = 'local-demo-placeholder'
                settings.litellm_api_base = 'https://unused.invalid/v1'
                settings.litellm_api_key = 'local-demo-placeholder'
                app.state.manager.temporal = env.client
                yield
    app.router.lifespan_context = lifespan

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE.split('<script>')[0] + '<script src="/demo/script.js"></script>'

    @app.get('/demo/script.js')
    async def script():
        return Response(PAGE.split('<script>')[1].split('</script>')[0], media_type='text/javascript')

    @app.post('/demo/step/{step}')
    async def step(step: int):
        async with lock:
            if step == 0:
                owner = app.state.store.identity({'method': 'local', 'role': 'admin'})
                run = app.state.store.create_run('Demonstrate schedule controls', '', 'modal', [], chat_enabled=True, user_id=owner)
                app.state.store.claim_message(run['id'])
                app.state.store.update_run(run['id'], status='running', token_hash=digest('local-demo-capability'))
                context.update(run=app.state.store.run(run['id']), step=0)
            if context.get('step') != step:
                raise HTTPException(409, 'Run the demo steps in order.')
            run = context['run']
            args = {'turn_id': run['active_message_id'], 'request_key': f'demo-action-{step}'}
            name = ['automation_list', 'automation_create', 'automation_enable', 'automation_update', 'automation_enable', 'automation_pause'][step]
            if step == 0:
                args = {}
            elif step == 1:
                args['definition'] = {'name': 'Weekly skills audit', 'prompt': 'Check for skills updates and report findings.',
                                      'timing': {'frequency': 'weekly', 'weekday': 1, 'time': '09:00', 'timezone': 'America/Los_Angeles'}}
            else:
                args.update(automation_id=context['result']['id'], revision=context['result']['revision'])
                if step == 3:
                    definition = context['result']['definition']
                    definition['triggers'][0]['schedule'].update(weekday=3, time='10:30')
                    args['definition'] = definition
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=values['public_url']) as client:
                response = await client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer local-demo-capability'},
                                             json={'name': name, 'arguments': args})
            if response.status_code != 200:
                raise HTTPException(response.status_code, response.json())
            result = response.json()
            context.update(result=result, step=step+1)
            if step == 0:
                summary = f"Found {len(result['automations'])} saved automations for this requester."
                state = 'Current user verified\nAutomation tools available'
            else:
                summary = f"Revision {result['revision']} · {result['status']}"
                if result['next_runs']:
                    upcoming = result['next_runs'][0]
                    summary += '\nNext run: ' + upcoming['local_time'] + ' · ' + upcoming['timezone']
                elif result['paused']:
                    summary += '\nNo future launches while paused.'
                state = result['definition']['name'] + '\n' + summary
            if step == 5:
                await app.state.automations.sync(app.state.manager.temporal, automation_id=result['id'])
                app.state.store.update_run(run['id'], status='completed', token_hash='')
            return {'tool': name, 'http_status': response.status_code, 'summary': summary, 'state': state}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-automation-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8793, log_level='warning')
