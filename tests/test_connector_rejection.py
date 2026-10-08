"""Policy rejection is actionable without poisoning subsequent agent work."""
import json
from http.server import BaseHTTPRequestHandler

import pytest
from app.security import digest

from test_broker_transport import diagnostic_relay
from test_workspace import cloud_capability, workspace


@pytest.mark.parametrize('case', ['scope', 'arguments', 'read_only', 'disabled'])
def test_connector_rejection_is_recoverable(workspace, monkeypatch, case):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    app.state.store.update_run(run_id, token_hash=digest('private-capability'))
    headers = {'Authorization': 'Bearer private-capability'}
    calls = []

    async def provider(name, arguments):
        calls.append(name)
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'call', provider)
    name, arguments = 'linear_search', {'query': 'fixture'}
    if case == 'scope':
        name, arguments = 'slack_send', {}
    elif case == 'arguments':
        name, arguments = 'linear_issue', {'issue_id': '../../private-marker'}
    elif case == 'read_only':
        name, arguments = 'linear_comment', {'issue_id': 'LIT-1', 'body': 'fixture'}
    if case in {'read_only', 'disabled'}:
        client.patch('/api/connections/linear/policy', json={'enabled': case != 'disabled', 'read_only': case == 'read_only'})

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            response = client.post(f'/broker/{run_id}/tools/call', headers={**headers, 'Content-Type': self.headers['Content-Type']},
                content=self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(response.status_code)
            for key in ('content-type', 'content-length', 'x-moyai-tool-error'):
                if key in response.headers:
                    self.send_header(key, response.headers[key])
            self.end_headers()
            self.wfile.write(response.content)

    with diagnostic_relay(Edge) as (relay, local, diagnostics):
        response = local.post('/tools/call', json={'name': name, 'arguments': arguments})
        assert response.status_code == 200
        assert response.json()['error']
        assert 'private-marker' not in response.text
        assert not calls
        assert not relay.last_error and not relay.last_failure and not relay.uncertain_tool
        assert not diagnostics
        client.patch('/api/connections/linear/policy', json={'enabled': True, 'read_only': False})
        assert local.post('/tools/call', json={'name': 'linear_search', 'arguments': {'query': 'fixture'}}).json() == {'ok': True}
        assert calls == ['linear_search']
        # A revoked run remains an authentication failure, not a recoverable rejection.
        app.state.store.update_run(run_id, token_hash='')
        assert local.post('/tools/call', json={'name': name, 'arguments': arguments}).status_code == 401
        assert relay.last_error and relay.uncertain_tool
