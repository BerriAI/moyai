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
            target = store.create_run('Saved investigation', '', 'demo', [], chat_enabled=True, user_id='google:demo')
            store.event(target['id'], 'tool', 'Synthetic request', {'tool': 'terminal', 'phase': 'completed',
                        'output': 'password=synthetic-redaction-canary'})
            caller = store.create_run('Read saved investigation', '', 'modal', [], chat_enabled=True, user_id='google:demo')
            store.claim_message(caller['id'])
            store.update_run(caller['id'], status='running', token_hash=digest('synthetic-demo-capability'))
            headers = {'Authorization': 'Bearer synthetic-demo-capability'}
            path = '/broker/' + caller['id']
            with httpx.Client(base_url=url, headers=headers) as client:
                catalog = client.get(path + '/tools')
                assert catalog.status_code == 200
                assert 'sessions_read' in {tool['name'] for tool in catalog.json()}
                for section in ('messages', 'activity'):
                    response = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {
                        'session_id': target['id'], 'section': section}})
                    assert response.status_code == 200, response.text
                    assert 'synthetic-redaction-canary' not in response.text
                    print(json.dumps({'section': section, 'http_status': response.status_code,
                                      'items': response.json()['items'], 'untrusted_reference': response.json()['untrusted_reference']}))
                hidden = store.create_run('Other owner', '', 'demo', [], user_id='google:other')
                response = client.post(path + '/tools/call', json={'name': 'sessions_read', 'arguments': {'session_id': hidden['id']}})
                assert response.status_code == 404
                print(json.dumps({'unauthorized_session_http_status': response.status_code}))
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            sock.close()
            assert not thread.is_alive(), 'Local server did not stop'


if __name__ == '__main__':
    main()
