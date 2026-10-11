"""Local demo: uv run python scripts/slack_thread_replies_demo.py.

Open http://127.0.0.1:8839/demo. Each example sends a signed HTTP request to
the real Slack webhook and reads the real SQLite input queue. Slack identities
are synthetic; model execution and outbound Slack delivery are disabled.
"""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings
from app.db import Store
from app.main import create_app


URL = 'http://127.0.0.1:8839'
SECRET = 'local-demo-signing-key'
ROOT = '1790719000.123456'
EXAMPLES = [
    ('Start the thread', '<@U99999999> Help me investigate this issue.'),
    ('Ordinary reply', 'Sure. Give me a list of steps to do'),
    ('Tag someone else', '<@U88888888> can you handle this?'),
    ('Include Moyai too', '<@U88888888> can you help? <@U99999999> list the steps.'),
]

PAGE = '''<!doctype html><html><head><meta charset="utf-8">
<title>Moyai Slack reply verification</title><style>
body{font:17px system-ui;margin:0;background:#f6f5f9;color:#221f30}
main{max-width:1080px;margin:40px auto;padding:0 24px}h1{font-size:36px;margin:10px 0}
.label{color:#6247aa;font-size:13px;font-weight:700;letter-spacing:1px}
p{color:#615b72;line-height:1.5}button{background:#5b3fd1;color:white;border:0;border-radius:8px;padding:14px 22px;font:600 16px system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:default}section{background:white;border:1px solid #e2dfeb;border-radius:12px;padding:24px;margin:22px 0}
pre{white-space:pre-wrap;font:15px/1.6 ui-monospace,monospace;margin:14px 0 0}
table{border-collapse:collapse;width:100%;font-size:15px}td,th{text-align:left;padding:14px 10px;border-bottom:1px solid #ece9f2}th{color:#6b637c}
.ok{color:#147242;font-weight:650}.skip{color:#865f1a;font-weight:650}#summary{font-size:20px;font-weight:650;color:#372577}#request{padding:14px;background:#f6f5f9;border-radius:8px}
</style></head><body><main><div class="label">LOCAL BACKEND DEMO · SIGNED HTTP WEBHOOK + REAL SQLITE</div>
<h1>Replies stay in the conversation</h1><p>Ordinary replies reach Moyai. Tagging someone else skips the message unless Moyai is also tagged.<br>Synthetic Slack events; no live Slack delivery or model execution.</p>
<section><strong id="next">1. Start the thread</strong><pre id="request">@Moyai Help me investigate this issue.</pre><p><button id="send">Send signed webhook</button></p><span id="status">Ready to send the first example.</span></section>
<section><table><thead><tr><th>Example</th><th>HTTP</th><th>Saved input</th><th>Session</th></tr></thead><tbody id="results"></tbody></table><p id="summary">0 inputs · 0 sessions</p><pre id="saved"></pre></section>
<p>Requests use POST /hooks/slack/events. Results are read from messages and slack_receipts.<br>The final check reopens the database and retries the ordinary reply to verify persistence and deduplication.</p>
</main><script src="/demo.js" defer></script></body></html>'''

SCRIPT = '''
const button=document.getElementById('send');
button.onclick=async()=>{
 button.disabled=true;
 try{
  const response=await fetch('/demo/next',{method:'POST'});
  if(!response.ok)throw new Error(await response.text());
  const result=await response.json();
  const tr=document.createElement('tr');
  for(const value of [result.label,result.http,result.result,result.session]){
   const td=document.createElement('td');td.textContent=value;tr.appendChild(td);
  }
  tr.children[2].className=result.result==='Skipped'?'skip':'ok';
  document.getElementById('results').appendChild(tr);
  document.getElementById('summary').textContent=result.summary;
  document.getElementById('saved').textContent=result.saved;
  document.getElementById('status').textContent=result.status;
  document.getElementById('next').textContent=result.next_label;
  document.getElementById('request').textContent=result.next_text;
  button.textContent=result.done?'Verification complete':result.next_button;
  button.disabled=result.done;
 }catch(error){document.getElementById('status').textContent=String(error);button.disabled=false;}
};
'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='https://workspace.example', agent_harness='hermes',
                  workspace_password='local-demo-password-only',
                  auto_prepare_repositories=False, session_titles_enabled=False,
                  slack_bot_enabled=True, slack_signing_secret=SECRET, slack_session_users='*',
                  modal_token_id='demo-only', modal_token_secret='demo-only',
                  litellm_api_key='demo-only', litellm_api_base='https://gateway.example/v1', agent_model='test-model')
    app = create_app(Settings(_env_file=None, **values))
    # Keep cloud readiness configured while serving this isolated demo locally.
    for middleware in app.user_middleware:
        if middleware.cls is TrustedHostMiddleware:
            middleware.kwargs['allowed_hosts'].append('127.0.0.1')
    store = app.state.store
    app.state.manager.submit = lambda run: None
    state = {'step': 0, 'run_id': ''}

    @asynccontextmanager
    async def lifespan(_):
        app.state.connectors.save('slack', {'kind': 'oauth', 'bot': {
            'access_token': 'local-demo-only', 'team': {'id': 'T12345678'},
            'bot_user_id': 'U99999999', 'scope': 'reactions:write'}}, 'Local demo')
        yield

    app.router.lifespan_context = lifespan

    def payload(index):
        return {'type': 'event_callback', 'team_id': 'T12345678', 'event_id': f'EvDemo{index}',
                'event': {'type': 'app_mention' if index == 0 else 'message',
                          'user': 'U12345678', 'channel': 'C12345678', 'thread_ts': ROOT,
                          'ts': f'179071900{index}.123456', 'text': EXAMPLES[index][1]}}

    async def deliver(index):
        body = json.dumps(payload(index)).encode()
        timestamp = str(int(time.time()))
        signature = 'v0=' + hmac.new(SECRET.encode(), b'v0:' + timestamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(base_url=URL) as client:
            response = await client.post('/hooks/slack/events', content=body, headers={
                'Content-Type': 'application/json', 'X-Slack-Request-Timestamp': timestamp,
                'X-Slack-Signature': signature})
            response.raise_for_status()
            return response.status_code

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE

    @app.get('/demo.js')
    async def script():
        return Response(SCRIPT, media_type='text/javascript')

    @app.post('/demo/next')
    async def next_example():
        step = state['step']
        if step >= 5:
            return {'done': True}
        before = len(store.rows('SELECT id FROM messages'))
        http = await deliver(step if step < 4 else 1)
        runs = store.rows('SELECT id FROM runs')
        run_id = runs[0]['id']
        if not state['run_id']:
            state['run_id'] = run_id
        assert run_id == state['run_id'] and len(runs) == 1
        messages = store.messages(run_id)
        added = len(messages) - before
        assert added == (1 if step in {0, 1, 3} else 0)
        result = 'Queued once' if added else 'Skipped'
        if step == 4:
            restored = Store(Path(directory))
            saved = restored.messages(run_id)
            assert len(saved) == 3 and saved[1]['content'].endswith(EXAMPLES[1][1])
            assert len(restored.rows('SELECT event_id FROM slack_receipts')) == 3
            result = 'Persisted · no duplicate'
        state['step'] += 1
        next_step = state['step']
        return {'label': EXAMPLES[step][0] if step < 4 else 'Reopen database + retry',
                'http': http, 'result': result, 'session': 'Same session' if step else 'Created',
                'summary': f'{len(messages)} inputs · {len(runs)} session · {len(store.rows("SELECT event_id FROM slack_receipts"))} receipts',
                'saved': 'Saved reply: ' + next((m['content'].split('\n')[-1] for m in messages if EXAMPLES[1][1] in m['content']), 'Waiting for ordinary reply'),
                'status': 'Database reopened; ordinary reply is still saved and retry added no duplicate.' if step == 4 else f'HTTP {http}; database change verified.',
                'next_label': f'{next_step + 1}. ' + EXAMPLES[next_step][0] if next_step < 4 else 'Verify saved reply after reopening the database' if next_step == 4 else 'All five checks passed',
                'next_text': EXAMPLES[next_step][1].replace('<@U99999999>', '@Moyai').replace('<@U88888888>', '@Teammate') if next_step < 4 else 'Reopen SQLite and redeliver the same ordinary reply.' if next_step == 4 else 'Ordinary reply accepted · other-person tag skipped · Moyai mention accepted · saved once',
                'next_button': 'Send signed webhook' if next_step < 4 else 'Reopen database and retry',
                'done': next_step == 5}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-slack-replies-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8839, log_level='warning')
