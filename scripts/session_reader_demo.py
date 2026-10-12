"""Exercise sessions_read over real local HTTP with synthetic, isolated storage."""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from app.config import Settings
from app.db import now
from app.main import create_app
from app.security import digest


def main():
    with tempfile.TemporaryDirectory() as directory:
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        app = create_app(Settings(_env_file=None, data_dir=Path(directory), public_url=url,
                                  workspace_password='synthetic-demo-password'))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]})
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started, 'Local server failed to start'
            store = app.state.store
            store.execute("INSERT INTO users(id,kind,name,created_at,updated_at) VALUES('google:demo','google','Demo',?,?)", (now(), now()))
            target = store.create_run('Saved investigation', '', 'demo', [], chat_enabled=True, user_id='google:demo')
            store.event(target['id'], 'tool', 'Synthetic request', {'tool': 'terminal', 'phase': 'completed',
                        'output': 'password=synthetic-redaction-canary'})
            store.event(target['id'], 'error', 'Saved broker failure', {'phase': 'broker_failure',
                        'http_status': 502, 'request_id': 'req-demo', 'raw_body': 'synthetic-redaction-canary'})
            group = 'demo-group'
            store.execute('''INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at)
                VALUES(?,?,1,'demo','{}','running',?)''', (group, target['id'], now()))
            child = store.create_run('Scout', '', 'demo', [], chat_enabled=True)
            store.execute("UPDATE runs SET parent_run_id=?,agent_group_id=?,agent_label='Scout' WHERE id=?",
                          (target['id'], group, child['id']))
            store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (child['id'],))
            store.update_run(child['id'], status='idle')
            store.execute('CREATE TABLE IF NOT EXISTS durable_sessions(run_id TEXT PRIMARY KEY,state TEXT)')
            store.execute('INSERT INTO durable_sessions(run_id,state) VALUES(?,?)',
                          (target['id'], json.dumps({'wait_group': group, 'private': 'synthetic-redaction-canary'})))
            store.update_run(target['id'], status='waiting_children')
            app.state.session_lifecycle.archive(target['id'], 'google:demo', True)
            before = store.run(target['id'])
            caller = store.create_run('Read saved investigation', '', 'modal', [], chat_enabled=True, user_id='google:demo')
            store.claim_message(caller['id'])
            store.update_run(caller['id'], status='running', token_hash=digest('synthetic-demo-capability'))
            headers = {'Authorization': 'Bearer synthetic-demo-capability'}
            path = '/broker/' + caller['id']
            with httpx.Client(base_url=url, headers=headers) as client:
                catalog = client.get(path + '/tools')
                assert catalog.status_code == 200
                assert 'sessions_read' in {tool['name'] for tool in catalog.json()}
                response = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {
                    'session': url + '/#run=' + target['id'], 'limit': 1}})
                assert response.status_code == 200, response.text
                assert 'synthetic-redaction-canary' not in response.text
                body = response.json()
                assert body['agents']['waiting_group_id'] == group
                assert body['agents']['groups'][0]['current_settled']
                assert body['recent_failures'][0]['http_status'] == 502
                assert body['next_event']
                print(json.dumps({'http_status': response.status_code, **body}))
                page = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {
                    'session': target['id'], 'after_event': body['next_event'], 'limit': 1}})
                assert page.status_code == 200
                assert page.json()['events'][0]['id'] > body['events'][0]['id']
                assert store.run(target['id']) == before
                assert target['id'] in app.state.session_lifecycle.archives('google:demo')
                shared = store.create_run('Other owner', '', 'demo', [], user_id='google:other')
                response = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {'session': shared['id']}})
                assert response.status_code == 200
                store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), shared['id']))
                response = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {'session': shared['id']}})
                assert response.status_code == 404
                print(json.dumps({'pagination': 'passed', 'archive_unchanged': True,
                                  'shared_session_http_status': 200, 'deleting_session_http_status': response.status_code}))
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            sock.close()
            assert not thread.is_alive(), 'Local server did not stop'


if __name__ == '__main__':
    main()
