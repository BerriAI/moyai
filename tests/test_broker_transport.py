import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.security import digest
from sandbox.agent import conversation_prompt
from sandbox.broker_relay import BrokerRelay, EDGE_ERROR
from sandbox.broker_transport import CONTENT_TYPE, MAX_BODY, cipher, seal, unseal
from test_spend import active
from test_workspace import workspace, cloud_capability, wait_for

CODE = '''Repro: curl -s localhost:4100/openapi.json | python3 -c "import json,sys; print(json.load(sys.stdin)['paths'])"
Router already in sys.modules; check /v1/mcp/server/{server_id}/user-env-vars.
Fix merged: https://github.com/BerriAI/litellm/pull/38416
'''


def test_envelopes_are_bound_to_turn_capability_route_age_and_size():
    body = json.dumps({'messages': [{'role': 'tool', 'content': CODE}]}).encode()
    packet = seal('turn-one', '/v1/chat/completions', body)
    assert CODE.encode() not in packet
    assert unseal('turn-one', '/v1/chat/completions', packet) == body
    for token, path, value in [('turn-two', '/v1/chat/completions', packet),
                               ('turn-one', '/tools/call', packet),
                               ('turn-one', '/v1/chat/completions', packet[:-5] + b'AAAAA')]:
        with pytest.raises(ValueError):
            unseal(token, path, value)
    expired = cipher('turn-one').encrypt_at_time(b'/v1/chat/completions\n' + body, int(time.time()) - 301)
    with pytest.raises(ValueError):
        unseal('turn-one', '/v1/chat/completions', expired)
    with pytest.raises(ValueError):
        seal('turn-one', '/v1/chat/completions', b'x' * (MAX_BODY + 1))


@pytest.mark.parametrize('status', ['running', 'reconnecting'])
def test_sealed_model_keeps_content_model_pin_usage_and_access_checks(workspace, monkeypatch, status):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'existing-server-key'
    run = active(app)
    app.state.store.update_run(run["id"], status=status)
    received = []
    def upstream(request):
        assert request.headers['Authorization'] == 'Bearer existing-server-key'
        received.append(json.loads(request.content))
        return httpx.Response(200, headers={'x-litellm-response-cost': '0.000123'}, json={
            'choices': [{'message': {'role': 'assistant', 'content': 'Fix already merged'}}],
            'usage': {'prompt_tokens': 45, 'completion_tokens': 5, 'total_tokens': 50}})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    payload = {'model': 'unapproved-model', 'stream': True, 'messages': [{'role': 'user', 'content': CODE}]}
    packet = seal('capability', '/v1/chat/completions', json.dumps(payload).encode())
    endpoint = f"/broker/{run['id']}/v1/chat/completions"
    headers = {'Authorization': 'Bearer capability', 'Content-Type': CONTENT_TYPE}
    response = client.post(endpoint, content=packet, headers=headers)
    assert response.status_code == 200 and '[DONE]' in response.text
    assert received[0]['messages'] == payload['messages']
    assert received[0]['model'] == 'openai/gpt-6-astra'
    record = app.state.store.rows('SELECT * FROM model_requests')[0]
    assert record['user_id'] == 'google:alice' and record['cost'] == '0.000123'
    assert record['status'] == 'completed' and record['total_tokens'] == 50
    assert client.post(endpoint, content=packet, headers={**headers,'Authorization':'Bearer another'}).status_code == 401
    assert client.post(endpoint, content=seal('capability','/tools/call',b'{}'), headers=headers).status_code == 400
    assert client.post(endpoint, content=seal('capability','/v1/chat/completions',b'null'), headers=headers).status_code == 422
    app.state.store.update_run(run['id'], token_hash='')
    assert client.post(endpoint, content=packet, headers=headers).status_code == 401
    assert len(received) == 1


def test_sealed_tool_writes_still_wait_for_exact_admin_approval(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['slack'])
    calls = []
    async def send(name, arguments):
        calls.append((name,arguments))
        return {'ok': True}
    monkeypatch.setattr(app.state.connectors,'call',send)
    body = {'name': 'slack_send', 'arguments': {'channel': 'C12345678', 'text': CODE}}
    packet = seal('run-capability-only','/tools/call',json.dumps(body).encode())
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, f'/broker/{run_id}/tools/call', content=packet, headers={**headers,'Content-Type':CONTENT_TYPE})
        approval = wait_for(lambda: app.state.store.approvals(run_id))[0]
        assert not calls
        arguments = json.loads(approval['arguments']) if isinstance(approval['arguments'], str) else approval['arguments']
        assert arguments == body['arguments']
        assert client.post('/api/approvals/'+approval['id'], json={'decision':'deny'}).status_code == 200
        assert pending.result(timeout=3).json()['error'] == 'Action denied, expired, or cancelled.'
    assert not calls


def test_loopback_relay_seals_model_and_mcp_requests_and_returns_sse():
    requests = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            assert self.headers['Authorization'] == 'Bearer runtime-token'
            assert self.headers['Content-Type'] == CONTENT_TYPE
            path = self.path.removeprefix('/broker/run')
            body = unseal('runtime-token',path,self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((path,json.loads(body)))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(b'data: {"choices":[]}\n\ndata: [DONE]\n\n')
    server=ThreadingHTTPServer(('127.0.0.1',0),Edge)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    relay=BrokerRelay(f'http://127.0.0.1:{server.server_port}/broker/run','runtime-token').start()
    try:
        with httpx.Client(base_url=relay.url,timeout=5) as client:
            for path in ['/v1/chat/completions','/tools/call']:
                result=client.post(path,json={'content':CODE},headers={'Authorization':'Bearer runtime-token'})
                assert result.status_code==200 and '[DONE]' in result.text
            assert client.post('/v1/chat/completions',json={}).status_code==401
            assert client.post('/admin',json={},headers={'Authorization':'Bearer runtime-token'}).status_code==404
    finally:
        relay.close();server.shutdown();server.server_close();thread.join(timeout=2)
    assert requests==[(path,{'content':CODE}) for path in ['/v1/chat/completions','/tools/call']]


def test_credential_relay_pauses_and_exposes_only_scoped_sdk_paths():
    calls=[]
    request_id='a'*32
    class Edge(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            path=self.path.removeprefix('/broker/run')
            body=json.loads(unseal('runtime-token',path,self.rfile.read(int(self.headers['Content-Length']))))
            calls.append((path,body))
            result={'moyai_wait_credential':request_id} if path=='/tools/call' else {'data':[]}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Edge)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    relay=BrokerRelay(f'http://127.0.0.1:{server.server_port}/broker/run','runtime-token').start()
    try:
        with httpx.Client(base_url=relay.url,timeout=5) as client:
            headers={'Authorization':'Bearer runtime-token'}
            assert client.post('/tools/call',json={'name':'credentials_request','arguments':{}},headers=headers).status_code==200
            assert relay.wait_credential==request_id
            base='/credentials/'+request_id+'/v1'
            assert client.get(base+'/models',headers=headers).json()=={'data':[]}
            assert client.post(base+'/messages',json={'model':'claude-test'},headers={'x-api-key':'runtime-token'}).status_code==200
            assert client.post(base+'/chat/completions',json={'model':'gpt-test'},headers=headers).status_code==200
            assert client.get(base+'/models').status_code==401
            assert client.post(base+'/keys',json={},headers=headers).status_code==404
            assert client.get('/credentials/'+request_id+'/v1/models?token=x',headers=headers).status_code==404
        assert calls[1]==('/credentials/invoke',{'request_id':request_id,'method':'GET','path':'/models','body':{}})
        assert calls[2][1]['path']=='/messages' and calls[3][1]['path']=='/chat/completions'
        assert len(calls)==4
    finally:
        relay.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_edge_failure_is_actionable_and_does_not_guess_about_user_key():
    class Blocked(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.send_response(403);self.send_header('Content-Type','text/html');self.end_headers()
            self.wfile.write(b'<title>Blocked</title>')
    server=ThreadingHTTPServer(('127.0.0.1',0),Blocked)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    relay=BrokerRelay(f'http://127.0.0.1:{server.server_port}','runtime-token').start()
    try:
        result=httpx.post(relay.url+'/v1/chat/completions',json={},headers={'Authorization':'Bearer runtime-token'})
        assert result.status_code==502
        assert result.json()['error']['message']==EDGE_ERROR==relay.last_error
    finally:
        relay.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_slack_source_is_imported_once_and_current_followup_stays_current():
    spec={'prompt':'Find the existing PR', 'slack_source': {'messages':[{'text':'Issue LIT-6275'}]}}
    assert 'Issue LIT-6275' in conversation_prompt(spec)
    assert conversation_prompt(spec,has_history=True)=='Find the existing PR'


def test_failed_turn_is_not_saved_as_a_completed_assistant_answer(workspace):
    app,_=workspace
    run=app.state.store.create_run('A request','','modal',[],chat_enabled=True)
    message=app.state.store.claim_message(run['id'])
    app.state.store.finish_message(run['id'],message['id'],'Connection failed','failed')
    assert [m['status'] for m in app.state.store.messages(run['id'])]==['failed','failed']


def test_relay_only_pauses_for_an_authenticated_delegation_response():
    group = 'f' * 32
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            unseal('token', self.path, self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'moyai_wait_group': group}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token').start()
    try:
        headers = {'Authorization': 'Bearer token'}
        httpx.post(relay.url + '/tools/call', headers=headers, json={'name': 'slack_search'})
        assert relay.wait_group == ''
        response = httpx.post(relay.url + '/tools/call', headers=headers, json={'name': 'agents_fanout'})
        assert response.json()['moyai_wait_group'] == relay.wait_group == group
    finally:
        relay.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_relay_retries_only_explicit_unbilled_model_admission(monkeypatch):
    requests, envelopes = [], []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            envelope = self.rfile.read(int(self.headers['Content-Length']))
            envelopes.append(envelope)
            body = json.loads(unseal('token', self.path, envelope))
            requests.append(body)
            self.send_response(429 if len(requests) == 1 or body.get('generic_error') else 200)
            if len(requests) == 1:
                self.send_header('X-Moyai-Model-Queue', '1')
            self.end_headers()
            self.wfile.write(b'{"detail":"queued"}' if len(requests) == 1 else b'{"choices":[]}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token').start()
    monkeypatch.setattr('sandbox.broker_relay.time.sleep', lambda seconds: None)
    try:
        headers = {'Authorization':'Bearer token'}
        assert httpx.post(relay.url + '/v1/chat/completions', headers=headers, json={'messages':[]}).status_code == 200
        assert len(requests) == 2 and requests[0] == requests[1]
        assert envelopes[0] != envelopes[1]
        assert httpx.post(relay.url + '/v1/chat/completions', headers=headers, json={'generic_error':True}).status_code == 429
        assert len(requests) == 3
    finally:
        relay.close();server.shutdown();server.server_close();thread.join(timeout=2)


def test_full_model_capacity_returns_unbilled_admission_marker(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    run = active(app)
    received = []
    release = threading.Event()
    async def upstream(request):
        received.append(request)
        import asyncio
        while not release.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, json={'choices':[]})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    endpoint = f"/broker/{run['id']}/v1/chat/completions"
    headers = {'Authorization':'Bearer capability'}
    with ThreadPoolExecutor(max_workers=9) as pool:
        calls = [pool.submit(client.post, endpoint, json={'messages':[]}, headers=headers) for _ in range(8)]
        try:
            wait_for(lambda: len(received) == 8)
            queued = client.post(endpoint, json={'messages':[]}, headers=headers)
            assert queued.status_code == 429 and queued.headers['X-Moyai-Model-Queue'] == '1'
            assert len(app.state.store.rows('SELECT * FROM model_requests')) == 8
        finally:
            release.set()
        assert all(call.result(timeout=5).status_code == 200 for call in calls)


def test_bootstrap_relay_retries_reads_but_never_retries_submitted_posts():
    counts = {'GET': 0, 'POST': 0}
    notices = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            counts['GET'] += 1
            self.send_response(503 if counts['GET'] == 1 else 200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"tools":[]}')
        def do_POST(self):
            counts['POST'] += 1
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"temporary outage"}}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token', notify=notices.append).start()
    try:
        with httpx.Client(base_url=relay.url, timeout=5) as client:
            headers = {'Authorization': 'Bearer token'}
            assert client.get('/tools', headers=headers).json() == {'tools': []}
            assert counts['GET'] == 2 and len(notices) == 2
            assert relay.startup_failure is None
            for path in ['/v1/chat/completions', '/tools/call']:
                assert client.post(path, json={}, headers=headers).status_code == 503
            assert counts['POST'] == 2
    finally:
        relay.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_superseded_model_wait_never_submits_another_gateway_call(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled generation submitted a new model request')
    monkeypatch.setattr('sandbox.broker_relay.urllib.request.urlopen', forbidden)
    relay = BrokerRelay('http://unused.example', 'token').start()
    relay.steering = SimpleNamespace(model_wait=nullcontext, requested=True)
    try:
        with httpx.Client(base_url=relay.url, timeout=2) as client:
            response = client.post('/v1/chat/completions', json={}, headers={'Authorization':'Bearer token'})
            assert response.status_code == 409
    finally:
        relay.close()


def test_durable_relay_retries_disconnect_and_partial_body_with_same_id(monkeypatch):
    import socket
    requests = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(unseal('token', self.path, self.rfile.read(int(self.headers['Content-Length']))))
            requests.append(body)
            if len(requests) == 1:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            content = b'{"id":"saved","choices":[]}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content[:8] if len(requests) == 2 else content)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token', durable_inference=True).start()
    monkeypatch.setattr('sandbox.broker_relay.time.sleep', lambda seconds: None)
    try:
        response = httpx.post(relay.url + '/v1/chat/completions', headers={'Authorization':'Bearer token'}, json={'messages':[]})
        assert response.status_code == 200 and response.json()['id'] == 'saved'
        assert len(requests) == 3
        assert len({r['inference_id'] for r in requests}) == 1
        assert len(requests[0]['inference_id']) == 32
    finally:
        relay.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_durable_relay_pending_retries_but_terminal_error_does_not(monkeypatch):
    requests = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(json.loads(unseal('token', self.path, self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(503 if len(requests) == 1 else 400)
            if len(requests) == 1:
                self.send_header('X-Moyai-Inference-Pending', '1')
            self.end_headers()
            self.wfile.write(b'{"detail":"Model outcome is unconfirmed"}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token', durable_inference=True).start()
    monkeypatch.setattr('sandbox.broker_relay.time.sleep', lambda seconds: None)
    try:
        response = httpx.post(relay.url + '/v1/chat/completions', headers={'Authorization':'Bearer token'}, json={'messages':[]})
        assert response.status_code == 400
        assert response.headers['x-should-retry'] == 'false'
        assert len(requests) == 2 and requests[0]['inference_id'] == requests[1]['inference_id']
    finally:
        relay.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
