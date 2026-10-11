"""Local notification demo using real preferences, sessions and delivery worker.

Run: uv run python scripts/slack_notifications_demo.py --port 8846
Open /demo/login, then /demo/notifications. Notifications are already on by default.
Slack transport and login use synthetic fixtures; no external writes are made.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import HTMLResponse, RedirectResponse, Response
import uvicorn

from app.config import Settings
from app.db import database, now
from app.main import create_app


def demo(directory, port):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}',
        agent_harness='hermes', agent_model='demo-model', google_client_id='local-demo', google_client_secret='local-demo',
        google_allowed_domains='example.com', google_admin_emails='alex@example.com',
        auto_prepare_repositories=False, session_titles_enabled=False, slack_bot_enabled=True, slack_session_users='*')
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    owner = store.identity({'method': 'google', 'identity': {'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex'}})
    with store.connect() as conn:
        slack_user = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute("""UPDATE users SET email='alex@example.com',linked_user_id=?,profile_eligible=1,
            profile_checked_at=?,profile_next_check=9999999999,link_status='linked' WHERE id=?""", (owner, now(), slack_user))
    app.state.connectors.save('slack', {'kind': 'oauth', 'access_token': 'unused-local-fixture',
        'bot': {'access_token': 'local-fixture', 'team': {'id': 'T12345678'}, 'bot_user_id': 'U99999999',
                'scope': 'chat:write,im:write,users:read,users:read.email'}}, 'Local demo')
    delivered, scenario = [], {}

    async def transport(method, url, **kwargs):
        if url.endswith('conversations.open'):
            assert kwargs['json']['users'] == 'U12345678'
            return {'ok': True, 'channel': {'id': 'D12345678'}}
        if url.endswith('chat.postMessage'):
            delivered.append(kwargs['json'])
            return {'ok': True, 'ts': f'1790719999.{len(delivered):06d}'}
        raise RuntimeError('This demo cannot contact external services.')

    app.state.connectors.request = transport
    app.state.manager.submit = lambda run: None

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/#settings')
        app.state.security.new_session(response, identity={'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response

    @app.get('/demo/notifications')
    async def page():
        return HTMLResponse(PAGE)

    @app.get('/demo/notifications.js')
    async def script():
        return Response(SCRIPT, media_type='text/javascript')

    @app.get('/demo/state')
    async def state():
        return {'enabled': app.state.user_preferences.get(owner)['slack_notifications'], 'delivered': delivered,
                'run_id': scenario.get('run_id'), 'source': scenario.get('source', ''),
                'state': store.run(scenario['run_id'])['status'] if scenario else 'No session yet'}

    @app.post('/demo/scenario/{action}')
    async def action(action: str):
        def apply():
            if action in {'start', 'start-slack'}:
                if action == 'start-slack':
                    from uuid import uuid4
                    run = store.create_slack_run(uuid4().hex, 'Review checkout rollout in Slack', [],
                        'C12345678', '1790719999.000001', 'U12345678', team_id='T12345678', model='demo-model')
                else:
                    run = store.create_run('Review checkout rollout', '', 'demo', [], chat_enabled=True, user_id=owner)
                message = store.claim_message(run['id'])
                store.update_run(run['id'], status='running')
                scenario.update(run_id=run['id'], message_id=message['id'], source='Slack' if action == 'start-slack' else 'Web')
            elif action == 'approval' and scenario:
                store.execute("""INSERT INTO approvals VALUES(?,?,'demo.action','{}','pending',?,'')
                    ON CONFLICT(id) DO NOTHING""", ('demo-' + scenario['run_id'], scenario['run_id'], now()))
                store.update_run(scenario['run_id'], status='awaiting_approval')
            elif action == 'finish' and scenario:
                store.execute("UPDATE approvals SET status='approved' WHERE run_id=?", (scenario['run_id'],))
                store.finish_message(scenario['run_id'], scenario['message_id'],
                    'Local fixture: the rollout review is ready. Would you like to proceed?')
                store.update_run(scenario['run_id'], status='idle')
            elif action != 'poll':
                raise ValueError('Start a session first.')
        await database(apply)
        await database(app.state.slack.notifications.collect)
        await app.state.slack.notifications.deliver_one()
        return await state()

    return app


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moyai · Notification delivery demo</title><style>
*{box-sizing:border-box}body{background:#0b1220;color:#edeff4;font:16px/1.5 system-ui;margin:0;padding:48px}main{max-width:1100px;margin:auto}
h1{font-size:32px;margin:0 0 8px}h2{font-size:21px;margin:0 0 18px}p{color:#b4c0d5}a{color:#a8d9ff}header{margin-bottom:32px}
.grid{display:grid;grid-template-columns:1fr 1.2fr;gap:24px}.panel{background:#131f33;border:1px solid #30405c;border-radius:12px;padding:24px}
button,.link{display:inline-block;background:#b7dfff;color:#10223a;border:0;border-radius:7px;padding:11px 15px;font:inherit;cursor:pointer;margin:4px 6px 4px 0;text-decoration:none}
button.secondary{background:#293952;color:#f0f4ff}.card{background:#1c2b43;border-left:4px solid #acd6ff;border-radius:8px;padding:20px;margin:14px 0}.card strong{font-size:19px}
.badge{display:inline-block;font-size:13px;background:#233b33;color:#bcf2d7;border-radius:6px;padding:5px 10px}.muted{font-size:14px;color:#a6b3c9}.status{font-size:20px;margin:22px 0}
@media(max-width:650px){body{padding:18px}.grid{grid-template-columns:1fr}h1{font-size:26px}}
</style><main><header><div class="badge">Local verification · synthetic session</div><h1>Moyai session notifications</h1>
<p>Real preferences, session state and delivery worker. Slack transport is captured locally; no messages are sent to Slack.</p></header>
<div class="grid"><section class="panel"><h2>Session controls</h2><p id="preference"></p><a href="/#settings">Open Moyai Settings</a>
<div id="state" class="status">Loading…</div><button id="start">Start web session</button><button id="start-slack" class="secondary">Start Slack session</button><button id="approval" class="secondary">Request approval</button>
<button id="finish" class="secondary">Finish reply</button><button id="poll" class="secondary">Check again</button>
<p class="muted">Web sessions notify their owner by default. Slack sessions keep updates in Slack, with no extra DM. “Check again” verifies that saved events are not delivered twice.</p><a id="session" hidden>Open session</a></section>
<section class="panel"><h2>Captured DM payloads <span id="count">0</span></h2><p class="muted">Recipient: Alex · D12345678</p><div id="inbox"><p>No alerts delivered yet.</p></div></section></div></main>
<script src="/demo/notifications.js"></script></html>'''

SCRIPT = '''const $=s=>document.querySelector(s);
function paint(data){$('#preference').textContent='Slack session notifications: '+(data.enabled?'On (default)':'Off — disabled in Settings');$('#state').textContent=(data.source?data.source+' session: ':'Session: ')+data.state;$('#count').textContent='('+data.delivered.length+')';
if(data.run_id){$('#session').hidden=false;$('#session').href='/#run='+data.run_id;}
const inbox=$('#inbox');inbox.replaceChildren();if(!data.delivered.length){inbox.textContent='No alerts delivered yet.';}
for(const payload of data.delivered){const card=document.createElement('article');card.className='card';
for(const [index,block]of payload.blocks.entries()){if(block.text){const text=document.createElement(index===0?'strong':'p');text.textContent=block.text.text;card.append(text);}
if(block.type==='actions'){for(const item of block.elements){const link=document.createElement('a');link.className='link';link.textContent=item.text.text;link.href=item.url;card.append(link);}}}
inbox.append(card);}}
for(const button of document.querySelectorAll('button'))button.addEventListener('click',async()=>{button.disabled=true;try{paint(await(await fetch('/demo/scenario/'+button.id,{method:'POST'})).json());}finally{button.disabled=false;}});
async function refresh(){paint(await(await fetch('/demo/state')).json());}refresh();setInterval(refresh,2000);'''


if __name__ == '__main__':
    import argparse
    from tempfile import TemporaryDirectory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8846)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-notifications-') as directory:
        print(f'Local demo: http://127.0.0.1:{args.port}/demo/login', flush=True)
        uvicorn.run(demo(directory, args.port), host='127.0.0.1', port=args.port)
