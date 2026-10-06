"""Run: uv run python scripts/slack_sender_demo.py; open http://127.0.0.1:8796/demo.

Real broker, stored chat identities and Slack connector; simulated Slack HTTP API.
No production credentials, model calls or messages to real people.
"""
import asyncio
import copy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response

from app.config import Settings
from app.main import create_app
from app.security import digest


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai Devin · Slack sender verification</title>
<style>
body{font:17px system-ui;background:#f7f6fb;color:#29243c;margin:0;padding:32px}main{max-width:1060px;margin:auto}h1{font-size:34px;line-height:1.2;margin:12px 0}p{color:#625d72;line-height:1.5}button{background:#5b3fd1;color:white;border:0;border-radius:9px;padding:12px 20px;font:inherit;cursor:pointer}button:disabled{opacity:.5}.badge{color:#5b3fd1;font-size:13px;font-weight:700;letter-spacing:.08em}.card{background:white;border:1px solid #ddd7ed;border-radius:12px;padding:18px;margin-top:16px}.title{font-weight:700}.message{font-size:23px;margin:12px 0;color:#245d42}.detail{font-size:14px;color:#625d72;white-space:pre-wrap;line-height:1.5}#status{margin-left:16px;color:#245d42;font-weight:600}footer{font-size:13px;color:#625d72;margin-top:20px}
</style><main><div class="badge">MOYAI DEVIN · LOCAL BROKER VERIFICATION</div>
<h1>Messages from the bot.<br>The person who asked, named in the text.</h1>
<p>Real requests through Moyai's broker and connector, using stored test profiles.<br>Slack's API is simulated; no real Slack messages are sent.</p>
<button id="run">Run sender checks</button><span id="status">Ready</span><div id="results"></div>
<footer>Shared chat: Moe and Tin take consecutive turns. Missing bot access must block sending.</footer></main>
<script>
document.querySelector('#run').onclick=async()=>{const button=document.querySelector('#run');button.disabled=true;document.querySelector('#results').replaceChildren();
try{for(let i=0;i<3;i++){document.querySelector('#status').textContent='Checking '+(i+1)+' of 3…';const response=await fetch('/demo/step/'+i,{method:'POST'});if(!response.ok)throw Error('HTTP '+response.status);const data=await response.json();const row=document.createElement('div');row.className='card';for(const [cls,text] of [['title',data.title],['message',data.message],['detail',data.detail]]){const item=document.createElement('div');item.className=cls;item.textContent=text;row.append(item)}document.querySelector('#results').append(row);await new Promise(resolve=>setTimeout(resolve,4000));}document.querySelector('#status').textContent='All 3 checks passed';}catch(error){document.querySelector('#status').textContent='Failed: '+error.message;}finally{button.disabled=false}};
</script>'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8796', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    credentials = {'access_token': 'local-shared-user-placeholder', 'kind': 'oauth',
                   'bot': {'access_token': 'local-bot-placeholder', 'scope': 'chat:write,im:write,im:history',
                           'bot_user_id': 'U99999999', 'team': {'id': 'T12345678'}}}
    state = {}
    lock = asyncio.Lock()
    original_client = httpx.AsyncClient

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE.split('<script>')[0] + '<script src="/demo/script.js"></script>'

    @app.get('/demo/script.js')
    async def script():
        return Response(PAGE.split('<script>')[1].split('</script>')[0], media_type='text/javascript')

    @app.post('/demo/step/{step}')
    async def step(step: int):
        async with lock:
            store = app.state.store
            if step == 0:
                for person in ['Moe', 'Tin']:
                    store.identity({'method': 'google', 'identity': {'sub': person.lower(),
                                   'email': person.lower() + '@example.test', 'name': person}})
                run = store.create_run('Send a message', '', 'modal', ['slack'],
                                       chat_enabled=True, user_id='google:moe')
                state.update(run_id=run['id'], next_step=0)
                app.state.connectors.save('slack', copy.deepcopy(credentials), 'Local test installation')
            if step not in {0, 1, 2} or state.get('next_step') != step:
                raise HTTPException(409, 'Run the checks in order.')
            run_id = state['run_id']
            actor = 'google:moe' if step == 0 else 'google:tin'
            if step:
                store.enqueue_message(run_id, 'Send another message', 'demo-' + str(step), user_id=actor)
            turn = store.claim_message(run_id)
            store.update_run(run_id, status='running', token_hash=digest('local-demo-capability'))
            requests = []

            def slack_api(request):
                assert request.url.host == 'slack.com'
                assert request.headers['Authorization'] == 'Bearer local-bot-placeholder'
                body = json.loads(request.content) if request.content else dict(request.url.params)
                requests.append({'method': request.url.path.rsplit('/', 1)[-1], **body})
                if request.url.path.endswith('conversations.open'):
                    return httpx.Response(200, json={'ok': True, 'channel': {'id': 'D12345678'}})
                if request.url.path.endswith('conversations.replies'):
                    return httpx.Response(200, json={'ok': True, 'messages': [
                        {'user': 'U99999999', 'text': requests[-2]['text']}]})
                assert request.url.path.endswith('chat.postMessage')
                return httpx.Response(200, json={'ok': True, 'channel': body['channel'],
                    'ts': '1790719999.123456', 'message': {'user': 'U99999999', 'text': body['text']}})

            def slack_client(**kwargs):
                return original_client(transport=httpx.MockTransport(slack_api), **kwargs)

            if step == 2:
                app.state.connectors.save('slack', {'access_token': credentials['access_token'], 'kind': 'oauth'}, 'User token only')
            async with original_client(transport=httpx.ASGITransport(app=app), base_url=values['public_url']) as client:
                with patch('app.connectors.httpx.AsyncClient', slack_client):
                    response = await client.post(f'/broker/{run_id}/tools/call',
                        headers={'Authorization': 'Bearer local-demo-capability'},
                        json={'name': 'slack_send', 'arguments': {'channel': 'U12345678', 'text': 'Hello from my chat'}})
                    if step < 2:
                        sent = response.json()
                        read = await client.post(f'/broker/{run_id}/tools/call',
                            headers={'Authorization': 'Bearer local-demo-capability'},
                            json={'name': 'slack_thread', 'arguments': {'channel': sent['channel'],
                                  'thread_ts': sent['ts'], 'as_bot': True}})
                        assert read.json()['messages'][0]['text'] == sent['message']['text']
            response.raise_for_status()
            result = response.json()
            if step < 2:
                name = 'Moe' if step == 0 else 'Tin'
                assert result['message']['user'] == 'U99999999'
                assert result['message']['text'] == name + ': Hello from my chat'
                assert [r['method'] for r in requests] == ['conversations.open', 'chat.postMessage', 'conversations.replies']
                output = {'title': name + ' requests a DM · delivered by Moyai Devin',
                          'message': result['message']['text'],
                          'detail': 'Bot DM D12345678 · conversations.open → chat.postMessage → conversations.replies\n'
                                    'Sender credential: bot · Requester: ' + name + (' · Same chat; creator remains Moe' if step == 1 else '')}
            else:
                assert 'Reconnect Slack' in result['error'] and not requests
                output = {'title': 'Bot removed · shared user credential still present', 'message': 'Send blocked',
                          'detail': result['error'] + '\n0 Slack requests · No fallback to the shared user'}
            store.finish_message(run_id, turn['id'], 'Local check completed')
            store.update_run(run_id, status='idle', token_hash='')
            state['next_step'] += 1
            return output

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-slack-sender-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8796)
