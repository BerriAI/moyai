"""Local verification: uv run python scripts/slack_mentions_demo.py.

Open http://127.0.0.1:8828/demo. Slack profiles are simulated; message ingestion,
name resolution, persistence, live refresh and the product UI use real app code.
No model, cloud worker or Slack messages are sent. All data is temporary.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app.config import Settings
from app.main import STATIC, create_app


TEAM, OWNER, SENDER, TARGET = 'T10000001', 'U10000001', 'U10000002', 'U10000003'
TEXT = f'cc: <@{TARGET}> can you add persistent memory'

CONTROLS = '''<aside style="position:fixed;bottom:0;left:0;right:0;z-index:1000;background:#29243c;color:white;padding:14px 24px;display:flex;align-items:center;gap:20px;font:14px system-ui">
<div><strong>LOCAL VERIFICATION</strong><br>Simulated Slack profile · real chat and name resolution</div>
<button style="color:#29243c" id="demo-resolve">Resolve mentioned teammate</button>
<button style="color:#29243c" id="demo-reset">Reset demo</button>
<strong id="demo-result">The CC mention is waiting for its Slack profile.</strong></aside>
<script src="/demo-controls.js" defer></script>'''

SCRIPT = '''
for(const action of ['resolve','reset'])document.getElementById('demo-'+action).onclick=async()=>{
  const button=document.getElementById('demo-'+action);button.disabled=true;
  try{
    const result=await api('/demo/'+action,{method:'POST',body:'{}'});
    document.getElementById('demo-result').textContent=action==='reset'
      ?'The CC mention is waiting for its Slack profile.'
      :'CC shows Tin Lo · sender Mateo · owner Ryan · original message preserved';
  }finally{button.disabled=false;}
};
'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8828', auto_prepare_repositories=False,
                  slack_bot_enabled=True, slack_identity_linking_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    store, connectors = app.state.store, app.state.connectors
    state = {}

    async def slack_profile(method, url, **kwargs):
        assert method == 'GET' and url == 'https://slack.com/api/users.info'
        assert kwargs['params'] == {'user': TARGET}
        assert kwargs['headers'] == {'Authorization': 'Bearer local-demo-only'}
        return {'ok': True, 'user': {'id': TARGET, 'team_id': TEAM, 'profile': {'real_name': 'Tin Lo'}}}

    connectors.request = slack_profile

    @asynccontextmanager
    async def lifespan(_):
        connectors.save('slack', {'kind': 'oauth', 'bot': {'access_token': 'local-demo-only',
            'team': {'id': TEAM}, 'bot_user_id': 'U99999999', 'scope': 'users:read'}}, 'Local demo')
        for index, (user, prompt) in enumerate([(OWNER, 'Can you add persistent memory?'), (SENDER, TEXT)]):
            app.state.slack.chat.accept(team=TEAM, event_id=f'LocalMention{index}', channel='C10000001',
                ts=f'179071900{index}.123456', root='1790719000.123456', user=user, prompt=prompt,
                mentioned=True, missing_cloud=False)
            run_id = store.rows('SELECT id FROM runs')[0]['id']
            message = store.claim_message(run_id)
            store.finish_message(run_id, message['id'], 'This conversation is saved.' if index == 0 else 'Ready for the next step.')
            store.update_run(run_id, status='idle')
        for user, name in [(OWNER, 'Ryan'), (SENDER, 'Mateo')]:
            store.execute('UPDATE users SET name=?,email=? WHERE id=?',
                          (name, name.lower() + '@example.com', f'slack:{TEAM}:{user}'))
        state['run_id'] = run_id
        yield

    app.router.lifespan_context = lifespan

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        # Fragment navigation keeps this local-only verification toolbar loaded.
        return (STATIC / 'index.html').read_text().replace('</body>', CONTROLS + '</body>').replace(
            '<head>', '<head><script src="/demo-location.js"></script>')

    @app.get('/demo-location.js')
    async def location():
        return Response(f'location.hash="run={state["run_id"]}";', media_type='text/javascript')

    @app.get('/demo-controls.js')
    async def controls():
        return Response(SCRIPT, media_type='text/javascript')

    @app.post('/demo/{action}')
    async def action(action: str, request: Request):
        app.state.security.require(request, admin=True, mutation=True)
        if action == 'resolve':
            await app.state.identities.sync_due()
        elif action == 'reset':
            store.execute("UPDATE slack_mention_names SET name='',next_check=0")
            store.event(state['run_id'], 'chat', 'Local demo reset')
        assert store.run(state['run_id'])['owner_id'] == f'slack:{TEAM}:{OWNER}'
        original = store.rows("SELECT content,user_id FROM messages WHERE run_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                              (state['run_id'],))[0]
        assert original == {'content': f'Slack reply from {SENDER}:\n{TEXT}', 'user_id': f'slack:{TEAM}:{SENDER}'}
        return {'run_id': state['run_id']}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-mentions-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8828, log_level='warning')
