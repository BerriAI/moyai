"""Run: uv run python scripts/model_tools_demo.py; open http://127.0.0.1:8795/demo.

Exercises real broker tools and forwarding against a local HTTP provider stub.
Tool choices are scripted: this verifies routing, not live model intent recognition.
No production credentials, external inference, Modal or Slack are used.
"""
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.config import Settings
from app.main import create_app
from app.security import digest


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai model switching · local broker demo</title>
<style>
body{font:17px system-ui;color:#262336;background:#f7f6fb;margin:0;padding:32px}main{max-width:1100px;margin:auto}h1{font-size:34px;margin:8px 0}p{color:#625d72;line-height:1.5;margin:12px 0}button{background:#5b3fd1;color:white;border:0;border-radius:9px;padding:13px 22px;font:inherit;cursor:pointer}button:disabled{opacity:.5}.badge{font-size:13px;letter-spacing:.08em;color:#5b3fd1;font-weight:700}.state{float:right;background:#eee9ff;padding:14px 24px;border-radius:12px;min-width:250px;white-space:pre-line}#steps{margin-top:20px}.step{padding:13px 18px;background:white;border:1px solid #e3dfee;border-radius:10px;margin:10px 0;display:grid;grid-template-columns:260px 1fr;gap:20px}.step code{display:block;color:#625d72;font-size:12px;margin-top:6px}.result{font-size:16px;white-space:pre-wrap;color:#215c45}footer{font-size:13px;color:#6b657b;margin-top:18px}
</style><main><div class="badge">MOYAI DEVIN · LOCAL BROKER DEMO</div>
<h1>Model switching that changes routing</h1>
<p>Example request: “Use GLM 5.3 and summarize this thread.”<br>Real broker requests below; tool choices are scripted and the model provider is a local stub.</p>
<div class="state" id="state">Ready · GPT-6 Astra</div><button id="run">Run broker demo</button><div id="steps"></div>
<footer>No production deployment or live GLM inference. This harness verifies model selection, forwarding, history and retries.</footer></main>
<script>
const names=['Discover enabled models','Verify the original route','Switch through the agent tool','Verify the next model request','Preserve queue and retry safely'];
document.querySelector('#run').onclick=async()=>{const button=document.querySelector('#run');button.disabled=true;document.querySelector('#steps').replaceChildren();
try{for(let i=0;i<5;i++){const row=document.createElement('div');row.className='step';const label=document.createElement('div');const title=document.createElement('strong');title.textContent=names[i];label.append(title);const code=document.createElement('code');label.append(code);const out=document.createElement('div');out.className='result';out.textContent='Sending request…';row.append(label,out);document.querySelector('#steps').append(row);
const response=await fetch('/demo/step/'+i,{method:'POST'});if(!response.ok){out.textContent='Request failed: HTTP '+response.status;break}const data=await response.json();code.textContent=data.request+' → HTTP '+data.http_status;out.textContent=data.summary;document.querySelector('#state').textContent=data.state;await new Promise(resolve=>setTimeout(resolve,3500));}}catch(error){document.querySelector('#state').textContent='Demo request failed.'}finally{button.disabled=false}};
</script>'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8795', auto_prepare_repositories=False,
                  agent_model='openai/gpt-6-astra', litellm_api_base='http://127.0.0.1:8795/demo-gateway',
                  litellm_api_key='local-demo-placeholder')
    app = create_app(Settings(_env_file=None, **values))
    context, received = {}, []
    lock = asyncio.Lock()

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE.split('<script>')[0] + '<script src="/demo/script.js"></script>'

    @app.get('/demo/script.js')
    async def script():
        return Response(PAGE.split('<script>')[1].split('</script>')[0], media_type='text/javascript')

    @app.post('/demo-gateway/chat/completions')
    async def provider(request: Request):
        body = await request.json()
        received.append(body)
        return {'id': 'local-stub', 'model': body['model'], 'choices': [{'message': {'role': 'assistant', 'content': 'Local stub response; no inference.'}}]}

    @app.post('/demo/step/{step}')
    async def step(step: int):
        async with lock:
            store = app.state.store
            if step == 0:
                actor = store.identity({'method': 'local', 'role': 'admin'})
                run = store.create_run('Use GLM 5.3 and summarize this thread.', '', 'modal', [], chat_enabled=True,
                                       user_id=actor, model='openai/gpt-6-astra')
                store.claim_message(run['id'])
                store.update_run(run['id'], status='running', token_hash=digest('local-demo-capability'))
                context.update(run=store.run(run['id']), step=0,
                               history=[{'role': 'user', 'content': run['prompt']}])
            if context.get('step') != step:
                raise HTTPException(409, 'Run the demo steps in order.')
            run = context['run']
            headers = {'Authorization': 'Bearer local-demo-capability'}
            arguments = {'turn_id': run['active_message_id'], 'model': 'GLM 5.3', 'request_key': 'demo-switch-glm'}
            request_name = 'model_list' if step == 0 else 'model_switch' if step in {2, 4} else 'chat/completions'
            if step == 4:
                store.enqueue_message(run['id'], 'Later task with Opus', 'later-demo-request', 'anthropic/claude-opus-5-5', run['active_user_id'])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url']) as client:
                if step in {0, 2, 4}:
                    response = await client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                                                 json={'name': request_name, 'arguments': {} if step == 0 else arguments})
                else:
                    response = await client.post(f"/broker/{run['id']}/v1/chat/completions", headers=headers,
                                                 json={'model': 'openai/gpt-6-astra', 'messages': context['history']})
            response.raise_for_status()
            result = response.json()
            if step == 0:
                summary = 'Enabled: ' + ', '.join(item['name'] for item in result['models']) + '\nActive: ' + result['active_model']
            elif step in {1, 3}:
                expected = 'openai/gpt-6-astra' if step == 1 else 'fireworks_ai/glm-5p3'
                # The broker can prepend requester-scoped memory/skill guidance.
                assert received[-1]['model'] == expected and received[-1]['messages'][-len(context['history']):] == context['history']
                summary = 'Provider received: ' + received[-1]['model'] + '\nConversation history preserved.'
            elif step == 2:
                assert result['active_model'] == 'fireworks_ai/glm-5p3'
                summary = 'Selected: ' + result['active_model'] + '\nEffective on the next model request, in the same task.'
            else:
                queued = store.messages(run['id'])[-1]
                assert result['replayed'] and queued['model'] == result['default_model'] == 'anthropic/claude-opus-5-5'
                summary = 'Retry recognized; no duplicate switch.\nQueued Opus task and its future preference preserved.'
                store.finish_message(run['id'], run['active_message_id'], 'Local broker demo completed.')
                store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run['id'],))
                store.update_run(run['id'], status='idle', token_hash='')
            context['step'] += 1
            fresh = store.run(run['id'])
            label = next(item['name'] for item in app.state.settings.model_choices() if item['id'] == fresh['active_model'])
            return {'request': request_name, 'http_status': response.status_code, 'summary': summary,
                    'state': 'Active model: ' + label + ('\nVerified forwarding + safe retries' if step == 4 else '')}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-model-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8795)
