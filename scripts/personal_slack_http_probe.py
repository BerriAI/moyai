"""Exercise the real application over a loopback HTTP socket; Slack is synthetic."""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import httpx
import uvicorn
from fastapi.responses import JSONResponse
from app.config import Settings
from app.main import create_app
from app.security import digest

with tempfile.TemporaryDirectory() as data_dir:
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    values = {k: f.get_default(call_default_factory=True) for k, f in Settings.model_fields.items()}
    values.update(data_dir=Path(data_dir), public_url=base, session_secret='http-fixture-only',
                  google_client_id='synthetic', google_client_secret='synthetic',
                  google_admin_emails='alice@berri.ai', slack_client_id='synthetic',
                  slack_client_secret='synthetic', session_titles_enabled=False,
                  memory_review_enabled=False, auto_prepare_repositories=False, litellm_api_key='')
    app = create_app(Settings(_env_file=None, **values))
    app.state.manager.submit = lambda run: None
    app.state.connectors.save('slack', {'access_token': 'fixture-org'}, 'Synthetic organization')
    upstream_identities = []
    def upstream(request):
        endpoint = request.url.path.rsplit('/', 1)[-1]
        if endpoint == 'oauth.v2.access':
            result = {'ok': True, 'team': {'id': 'T12345678'}, 'authed_user': {
                'id': 'U12345678', 'token_type': 'user', 'access_token': 'fixture-personal'}}
        elif endpoint == 'auth.test':
            result = {'ok': True, 'team_id': 'T12345678', 'user_id': 'U12345678', 'user': 'fixture'}
        elif endpoint == 'search.messages':
            identity = 'personal' if request.headers['authorization'] == 'Bearer fixture-personal' else 'organization'
            upstream_identities.append(identity)
            result = {'ok': True, 'messages': {'matches': [{'text': identity}]}}
        else:
            raise AssertionError('Unexpected synthetic upstream endpoint')
        return httpx.Response(200, json=result)
    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(upstream), **kw)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]))
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        def client_for(name):
            response = JSONResponse({})
            sid = app.state.security.new_session(response, identity={
                'sub': name, 'email': name+'@berri.ai', 'domain': 'berri.ai', 'name': name})
            cookie = response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0]
            return httpx.Client(base_url=base, cookies={'workspace_session': cookie},
                headers={'Origin': base, 'X-CSRF-Token': app.state.security.csrf(sid)})
        with client_for('alice') as alice, client_for('bob') as bob:
            start = alice.post('/api/connections/slack/personal/oauth')
            assert start.status_code == 200
            state = parse_qs(urlparse(start.json()['url']).query)['state'][0]
            callback = alice.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'synthetic'})
            assert callback.status_code == 303
            created = alice.post('/api/runs', json={'prompt': 'socket-private-sentinel', 'private_session': True, 'plugins': ['slack']})
            assert created.status_code == 201, created.text
            run = created.json()
            app.state.store.claim_message(run['id'])
            app.state.store.execute("UPDATE runs SET mode='modal',status='running',token_hash=? WHERE id=?", (digest('synthetic-capability'), run['id']))
            def search():
                return alice.post('/broker/'+run['id']+'/tools/call', headers={'Authorization': 'Bearer synthetic-capability'},
                                  json={'name': 'slack_search', 'arguments': {'query': 'fixture'}})
            selected = search()
            assert selected.json()['messages']['matches'][0]['text'] == 'personal'
            rejected = bob.get('/api/runs/'+run['id'])
            assert rejected.status_code == 404
            assert 'socket-private-sentinel' not in bob.get('/api/runs?search=socket-private').text
            assert alice.delete('/api/connections/slack/personal').status_code == 200
            fallback = search()
            assert fallback.json()['messages']['matches'][0]['text'] == 'organization'
            assert alice.get('/api/runs/'+run['id']).json()['private_owner_id'] == 'google:alice'
            print(json.dumps({'transport': 'real loopback HTTP / uvicorn', 'upstream': 'synthetic Slack only',
                'oauth_callback_status': callback.status_code, 'private_creation_status': created.status_code,
                'other_user_read_status': rejected.status_code, 'other_user_search_redacted': True,
                'read_identities_before_after_disconnect': upstream_identities,
                'private_owner_preserved_after_disconnect': True}, indent=2))
    finally:
        server.should_exit = True
        thread.join(15)
        httpx.AsyncClient = original
        assert not thread.is_alive()
