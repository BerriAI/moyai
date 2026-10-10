import json
import errno
import socket
import threading
import time
import urllib.error
from contextlib import contextmanager
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.security import digest
from sandbox.agent import conversation_prompt
from sandbox.broker_relay import BrokerRelay, EDGE_ERROR
from sandbox.broker_transport import CONTENT_TYPE, MAX_BODY, cipher, seal, unseal
from sandbox.sdk_failure import codex_details
from sandbox.transport_recovery import retryable_failure
from test_spend import active
from test_workspace import workspace, cloud_capability, wait_for, recovery_catalog

CODE = '''Repro: curl -s localhost:4100/openapi.json | python3 -c "import json,sys; print(json.load(sys.stdin)['paths'])"
Router already in sys.modules; check /v1/mcp/server/{server_id}/user-env-vars.
Fix merged: https://github.com/BerriAI/litellm/pull/38416
'''


@pytest.mark.parametrize('sdk', ['codex', 'claude-agent-sdk'])
def test_sdk_stream_failure_preserves_success_status_as_response_context(sdk):
    from types import SimpleNamespace
    from sandbox.sdk_failure import failure_diagnostic, failure_summary
    failure = {'http_status': 200, 'request_id': 'request-123'}
    agent = SimpleNamespace(journal=SimpleNamespace(pending={}),
                            context=SimpleNamespace(relay=SimpleNamespace(last_failure=failure)))
    diagnostic = failure_diagnostic(agent, sdk, {'http_status': 200, 'code': 'responseStreamDisconnected'}
        if sdk == 'codex' else {'http_status': 200, 'sdk_error': 'server_error'})
    assert diagnostic['response_status'] == 200 and 'http_status' not in diagnostic
    assert 'HTTP 200' not in failure_summary(diagnostic)
    assert 'HTTP 200' not in failure_summary({**diagnostic, 'http_status': 200})
    assert failure['http_status'] == 200, 'The transport/recovery descriptor keeps the actual wire status'


@contextmanager
def diagnostic_relay(handler, *, remote=None):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    diagnostics = []
    relay = BrokerRelay(remote or f'http://127.0.0.1:{server.server_port}', 'private-capability',
                        report_error=diagnostics.append).start()
    try:
        with httpx.Client(base_url=relay.url, headers={'Authorization': 'Bearer private-capability'}, timeout=5) as client:
            yield relay, client, diagnostics
    finally:
        relay.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('receipt', [None, 'a' * 32, 'private-invalid-receipt'])
def test_local_input_rejection_preserves_only_a_safe_receipt(receipt):
    from sandbox.broker_relay import InputPending
    with diagnostic_relay(BaseHTTPRequestHandler) as (relay, client, diagnostics):
        def reject(request):
            raise InputPending(receipt)
        relay.before_model = reject
        response = client.post('/v1/messages', json={'messages': ['private-prompt']})
        assert response.status_code == 400
        error = response.json()['error']
        assert error['code'] == 'moyai_input_pending'
        assert error['message'] == ('A queued input is ready at this model boundary.'
                                    + (' Receipt: ' + receipt if receipt == 'a' * 32 else ''))
        assert 'private' not in response.text
        assert not diagnostics and relay.last_failure is None and not relay.model_failed


def test_refused_connection_preserves_cause_type_and_errno_without_endpoint():
    # Reserve a port until the test servers have bound their own ports, then
    # close it so the relay encounters a real refused connection.
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1', 0))
        endpoint = f'http://127.0.0.1:{unavailable.getsockname()[1]}/private-remote-path'
        with diagnostic_relay(BaseHTTPRequestHandler, remote=endpoint) as (relay, client, diagnostics):
            unavailable.close()
            assert relay.model_ready(timeout=0.1) is False
            assert not diagnostics and relay.last_failure is None
            assert client.post('/v1/messages', json={'messages': ['private-prompt']}).status_code == 502
            saved = relay.last_failure
            assert len(diagnostics) == 1 and saved == diagnostics[0]
            assert saved['error_type'] == 'URLError' and saved['cause_type'] == 'ConnectionRefusedError'
            assert saved['errno'] == errno.ECONNREFUSED
            assert saved['route'] == '/v1/messages' and saved['transient']
            assert 'private' not in json.dumps(saved) and '127.0.0.1' not in json.dumps(saved)


@pytest.mark.parametrize('native_receipt', [False, True])
def test_failure_preserves_request_ids_without_payloads_and_blocks_sdk_resend(native_receipt):
    from sandbox.broker_relay import InputPending
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.headers['X-Moyai-Request-ID'])
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(502)
            self.send_header('Content-Type', 'text/html')
            self.send_header('X-Request-ID', 'provider-123')
            self.send_header('Rndr-Id', 'render-456')
            self.send_header('X-Render-Request-ID', 'invalid value private-header')
            self.send_header('X-Private', 'private-header')
            self.end_headers()
            self.wfile.write(b'<html>private-provider-body</html>')
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"tools":[]}')
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.post('/v1/messages', json={'messages': ['private-prompt']}).status_code == 502
        assert len(diagnostics) == 1
        saved = diagnostics[0]
        assert saved == relay.last_failure
        assert saved['request_id'] == UUID(calls[0]).hex
        assert saved['request_ids'] == {'x-request-id': 'provider-123', 'rndr-id': 'render-456'}
        assert saved['http_status'] == 502 and saved['upstream_status'] is None
        assert saved['route'] == '/v1/messages' and saved['error_type'] == 'HTTPError'
        assert saved['transient'] and not saved['response_started'] and not saved['uncertain_tool']
        assert saved['response_bytes'] == len(b'<html>private-provider-body</html>')
        assert 'private' not in json.dumps(saved)
        assert client.get('/tools').status_code == 200
        assert relay.last_failure == saved and relay.last_error
        # The SDK may automatically retry 502; it must not resubmit inference.
        if native_receipt:
            def blocked_receipt() -> InputPending:
                return InputPending('a' * 32)
            relay.on_model_blocked = blocked_receipt
        blocked = client.post('/v1/messages', json={'messages': ['private-prompt']})
        assert blocked.status_code == (400 if native_receipt else 409)
        if native_receipt:
            assert blocked.json()['error']['message'] == InputPending('a' * 32).message()
        assert relay.last_failure is saved and relay.model_failed
        assert len(calls) == 1 and len(diagnostics) == 1
        # A live native continuation must explicitly consume this exact failure.
        assert not relay.resume_model(dict(saved))
        assert relay.resume_model(saved)
        assert not relay.last_error and relay.last_failure is None and not relay.model_failed
        assert not relay.resume_model(saved)
        assert client.post('/v1/messages', json={}).status_code == 502
        assert len(calls) == 2
        current = relay.last_failure
        assert not relay.resume_model(saved) and relay.last_failure is current
        relay.uncertain_tool = True
        assert not relay.resume_model(current) and relay.model_failed


@pytest.mark.parametrize('status,upstream,transient', [(502, 401, False), (502, 403, False),
    (502, 429, True), (502, 503, True), (429, None, False), (425, None, True),
    (524, None, True), (502, 524, True), (525, None, False), (400, None, False),
    (403, None, False), (429, 429, True), (503, 503, True)])
def test_failure_classification_uses_original_upstream_status(status, upstream, transient):
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(status)
            if upstream:
                self.send_header('X-Moyai-Upstream-Status', str(upstream))
            self.send_header('X-Moyai-Model-Request-ID', 'ledger-id')
            self.send_header('X-Moyai-Error-Code', 'rate_limit_error' if upstream == 429 else 'unknown')
            self.send_header('X-Moyai-Error-Stage', 'upstream')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"detail":"Model gateway rejected the request."}')
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.post('/v1/responses', json={}).status_code == status
        assert relay.last_failure['upstream_status'] == upstream
        assert relay.last_failure['stage'] == 'upstream'
        assert relay.last_failure['error_code'] == ('rate_limit_error' if upstream == 429 else 'unknown')
        assert relay.last_failure['request_ids']['x-moyai-model-request-id'] == 'ledger-id'
        assert relay.last_failure['transient'] is transient
        assert relay.resume_model(relay.last_failure) is transient
        assert bool(relay.last_error) is (not transient)


@pytest.mark.parametrize('route,uncertain_tool', [('/v1/messages', False), ('/tools/call', True),
    ('/credentials/materialize', True), ('/credentials/' + 'a' * 32 + '/v1/messages', True)])
def test_dropped_connection_is_recorded_once_without_replaying_tools(route, uncertain_tool):
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.close_connection = True
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.post(route, json={}).status_code == 502
        assert len(calls) == len(diagnostics) == 1
        saved = relay.last_failure
        assert saved['error_type'] == 'RemoteDisconnected'
        assert saved['http_status'] is None and saved['response_bytes'] == 0
        assert saved['transient'] and not saved['response_started']
        assert saved['uncertain_tool'] is uncertain_tool and relay.uncertain_tool is uncertain_tool
        assert relay.resume_model(saved) is (not uncertain_tool)


@pytest.mark.parametrize('name', ['agents_results', 'github_repositories'])
@pytest.mark.parametrize('fault', ['http', 'cloudflare', 'disconnect', 'partial', 'permanent', 'exhausted'])
def test_agent_read_reconnect_uses_broker_catalog_without_poisoning_model(recovery_catalog, monkeypatch, name, fault):
    catalog = recovery_catalog
    safe = {tool['name'] for tool in catalog if tool.get('annotations', {}).get('idempotentHint') is True}
    calls = []
    if fault == 'exhausted':
        from sandbox.startup import _read_with_reconnect
        monkeypatch.setattr('sandbox.broker_relay._read_with_reconnect',
            lambda request, reader, **options: _read_with_reconnect(request, reader, **{**options, 'budget': 0}))

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(catalog).encode())
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            calls.append((self.path, body))
            if self.path == '/v1/responses':
                self.send_response(502); self.end_headers(); return
            if len(calls) == 1:
                if fault == 'disconnect':
                    self.close_connection = True; return
                if fault == 'partial':
                    self.send_response(200); self.send_header('Content-Length', '100')
                    self.end_headers(); self.wfile.write(b'{"incomplete":')
                    self.close_connection = True; return
                self.send_response(403 if fault == 'permanent' else 524 if fault == 'cloudflare' else 502)
                self.end_headers(); return
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"completed":2}')

    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.get('/tools').status_code == 200
        body = {'name': name, 'arguments': {'group_id': 'a' * 32} if name == 'agents_results' else {}}
        response = client.post('/tools/call', json=body)
        recovers = fault not in {'permanent', 'exhausted'}
        expected = 200 if recovers else 503 if fault == 'exhausted' else 502
        assert response.status_code == expected, (response.status_code, relay.last_failure)
        assert name in safe and relay.retry_safe_tools == safe
        assert len(calls) == (2 if recovers else 1)
        assert all(call == ('/tools/call', body) for call in calls)
        if recovers:
            assert response.json() == {'completed': 2}
        assert not relay.uncertain_tool and not relay.last_error and relay.last_failure is None
        assert all(not diagnostic['uncertain_tool'] for diagnostic in diagnostics)
        # Reproduce the second half of the incident, then use the real model
        # gate: a preceding safe read must not veto live/checkpoint recovery.
        assert client.post('/v1/responses', json={}).status_code == 502
        failed_model = relay.last_failure
        assert client.post('/tools/call', json=body).status_code == 200
        assert relay.last_failure is failed_model and relay.model_failed
        assert relay.resume_model(failed_model)


def test_entire_broker_catalog_replays_only_declared_reads(recovery_catalog):
    attempts = {}
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(recovery_catalog).encode())
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            name = body['name']
            attempts[name] = attempts.get(name, 0) + 1
            self.send_response(524 if attempts[name] == 1 else 200)
            self.end_headers(); self.wfile.write(b'{"ok":true}')
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.get('/tools').status_code == 200
        safe = relay.retry_safe_tools
        # Complete classification is independently pinned at the producer seam.
        # Run safe reads first, then prove writes cannot borrow their permission.
        names = sorted(safe) + sorted({tool['name'] for tool in recovery_catalog} - safe)
        for name in names:
            response = client.post('/tools/call', json={'name': name, 'arguments': {}})
            assert response.status_code == (200 if name in safe else 524), name
            assert attempts[name] == (2 if name in safe else 1), name
            assert relay.uncertain_tool is (name not in safe), name
        assert all(item['uncertain_tool'] for item in diagnostics)


@pytest.mark.parametrize('independent', ['model', 'write'])
@pytest.mark.parametrize('read_fails', [False, True])
def test_inflight_safe_read_preserves_independent_failure(recovery_catalog, independent, read_fails):
    arrived, release = threading.Event(), threading.Event()
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(recovery_catalog).encode())
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            calls.append((self.path, body))
            if body.get('name') == 'github_repositories':
                arrived.set()
                assert release.wait(5)
                self.send_response(403 if read_fails else 200)
            else:
                self.send_response(524)
            self.end_headers(); self.wfile.write(b'{}')
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.get('/tools').status_code == 200
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, '/tools/call', json={'name': 'github_repositories', 'arguments': {}})
            try:
                assert arrived.wait(5)
                route = '/v1/responses' if independent == 'model' else '/tools/call'
                assert client.post(route, json={'name': 'slack_send', 'arguments': {}}).status_code == 524
                saved, error = relay.last_failure, relay.last_error
            finally:
                release.set()
            assert pending.result().status_code == (502 if read_fails else 200)
        assert len(calls) == 2 and relay.last_failure is saved and relay.last_error == error
        assert relay.uncertain_tool is (independent == 'write')
        assert relay.resume_model(saved) is (independent == 'model')
        if read_fails:
            assert diagnostics[-1]['uncertain_tool'] is False


def test_stop_revokes_safe_read_during_reconnect(workspace):
    app, broker = workspace
    run_id, headers = cloud_capability(app, ['github'])
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            response = broker.get(f'/broker/{run_id}/tools', headers=headers)
            self.send_response(response.status_code); self.end_headers()
            self.wfile.write(response.content)
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            calls.append(body)
            if len(calls) == 1:
                app.state.store.update_run(run_id, status='stopping')
                self.send_response(524); self.end_headers(); return
            response = broker.post(f'/broker/{run_id}/tools/call', headers=headers, json=body)
            self.send_response(response.status_code); self.end_headers()
            self.wfile.write(response.content)
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.get('/tools').status_code == 200
        assert client.post('/tools/call', json={'name': 'github_repositories', 'arguments': {}}).status_code == 401
        assert len(calls) == 2 and not relay.uncertain_tool and not relay.last_error
        assert diagnostics[-1]['http_status'] == 401


@pytest.mark.parametrize('metadata', ['missing', 'read_only', 'string', 'duplicate', 'revoked', 'malformed', 'prior_write'])
def test_tool_retry_permission_cannot_come_from_arguments_or_stale_catalog(metadata, monkeypatch):
    hints = {'readOnlyHint': True, 'idempotentHint': True}
    catalog = [{'name': 'operation', 'annotations': dict(hints)}]
    if metadata == 'missing': catalog = []
    if metadata == 'read_only': catalog[0]['annotations'].pop('idempotentHint')
    if metadata == 'string': catalog[0]['annotations']['idempotentHint'] = 'true'
    if metadata == 'duplicate': catalog *= 2
    if metadata == 'malformed': catalog[0]['annotations'] = 'invalid'
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(catalog).encode())
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(502); self.end_headers()
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        assert client.get('/tools').status_code == 200
        if metadata == 'revoked':
            catalog.clear()
            assert client.get('/tools').status_code == 200
        assert client.post('/tools/call', json={'name': 'unknown' if metadata == 'prior_write' else 'operation', 'arguments': {},
            'annotations': hints, 'read_only': True}).status_code == 502
        assert len(calls) == 1 and relay.uncertain_tool
        assert diagnostics[-1]['uncertain_tool']
        assert client.get('/tools').status_code == 200
        assert relay.uncertain_tool  # Discovery cannot clear an uncertain write.
        if metadata == 'prior_write':
            from sandbox.startup import _read_with_reconnect
            monkeypatch.setattr('sandbox.broker_relay._read_with_reconnect',
                lambda request, reader, **options: _read_with_reconnect(request, reader, **{**options, 'budget': 0}))
            uncertain = relay.last_failure
            assert client.post('/tools/call', json={'name': 'operation', 'arguments': {}}).status_code == 503
            assert len(calls) == 2 and relay.uncertain_tool and relay.last_failure is uncertain


def test_partial_stream_is_recorded_without_sending_another_http_status():
    piece = b'event: message_start\ndata: {"partial":true}\n\n'
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(piece) + 100))
            self.send_header('X-Request-ID', 'partial-request')
            self.end_headers()
            self.wfile.write(piece)
            self.close_connection = True
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        response = client.post('/v1/messages', json={})
        assert response.status_code == 200 and response.content == piece
        assert len(diagnostics) == 1
        saved = relay.last_failure
        assert saved['response_started'] and saved['transient'] and saved['transport_interrupted']
        assert saved['response_bytes'] == len(piece) and saved['error_type'] == 'IncompleteRead'
        assert saved['request_ids'] == {'x-request-id': 'partial-request'}
        assert client.post('/v1/messages', json={}).status_code == 409
        assert not retryable_failure(saved) and not relay.resume_model(saved)
        assert retryable_failure(saved, live=True) and relay.resume_model(saved, live=True)
        assert len(diagnostics) == 1 and not relay.model_failed


@pytest.mark.parametrize('info,message,expected', [
    ('other', 'stream disconnected before completion: stream closed before response.completed', 'responseStreamDisconnected'),
    ({'other': None}, 'stream disconnected before completion: stream closed before response.completed', 'responseStreamDisconnected'),
    ('other', 'stream disconnected before completion: stream closed before response.completed private-detail', 'other'),
    ('other', 'private provider failure', 'other'),
    ('badRequest', 'stream disconnected before completion: stream closed before response.completed', 'badRequest'),
])
def test_clean_eof_normalizes_only_the_pinned_native_protocol_error(info, message, expected):
    assert codex_details({'codexErrorInfo': info, 'message': message}, will_retry=False) == {
        'source': 'native_error', 'code': expected, 'will_retry': False}


def test_native_eof_uses_only_the_current_responses_metadata_without_overwriting_failures():
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(unseal('private-capability', self.path,
                                    self.rfile.read(int(self.headers['Content-Length']))))
            calls.append((self.path, self.headers['X-Moyai-Request-ID']))
            self.send_response(502 if body.get('fail') else 200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('X-Request-ID', 'responses-edge-id')
            self.end_headers()
            self.wfile.write(b'event: response.created\ndata: {"private":"fixture"}\n\n')
    with diagnostic_relay(Edge) as (relay, client, diagnostics):
        relay.note_stream_disconnect()
        assert relay.last_failure is None and not diagnostics
        assert client.post('/v1/messages', json={}).status_code == 200
        relay.note_stream_disconnect()
        assert relay.model_response is None and relay.last_failure is None
        assert client.post('/v1/responses', json={}).status_code == 200
        observed = relay.model_response
        assert observed['route'] == '/v1/responses' and observed['http_status'] == 200
        assert observed['request_id'] == calls[-1][1]
        assert observed['request_ids'] == {'x-request-id': 'responses-edge-id'}
        assert observed['response_started'] and observed['transport_interrupted']
        assert 'private' not in json.dumps(observed)
        relay.uncertain_tool = True
        relay.note_stream_disconnect()
        assert relay.last_failure is None and not diagnostics
        relay.uncertain_tool = False
        relay.note_stream_disconnect()
        relay.note_stream_disconnect()
        assert relay.last_failure is observed and diagnostics == [observed]
        assert client.post('/v1/responses', json={}).status_code == 409 and len(calls) == 2
        assert not retryable_failure(observed) and not relay.resume_model(observed)
        assert retryable_failure(observed, live=True) and relay.resume_model(observed, live=True)
        assert client.post('/v1/responses', json={}).status_code == 200
        assert relay.model_response['request_id'] != observed['request_id']
        assert client.post('/v1/messages', json={'fail': True}).status_code == 502
        current = relay.last_failure
        relay.note_stream_disconnect()
        assert relay.last_failure is current and current['route'] == '/v1/messages'
        assert len(calls) == 4 and diagnostics == [observed, current]


@pytest.mark.parametrize('outcome', ['ready', 'unavailable', 'unauthorized', 'forbidden',
                                    'redirect', 'invalid', 'shape', 'oversize'])
def test_recovery_readiness_uses_one_authenticated_get_and_rejects_invalid_responses(outcome):
    requests = []
    status = {'unavailable': 503, 'unauthorized': 401, 'forbidden': 403, 'redirect': 302}.get(outcome, 200)
    body = {'invalid': b'not-json', 'shape': b'{"data":[]}', 'oversize': b' ' * 8193}.get(
        outcome, b'{"object":"list","data":[]}')
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append((self.command, self.path, self.headers['Authorization'], self.headers['X-Moyai-Request-ID']))
            self.send_response(status)
            if outcome == 'redirect':
                self.send_header('Location', '/must-not-receive-the-capability')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET
    with diagnostic_relay(Edge) as (relay, _, diagnostics):
        if outcome in {'ready', 'unavailable'}:
            assert relay.model_ready(timeout=0.5) is (outcome == 'ready')
        elif status != 200:
            with pytest.raises(urllib.error.HTTPError) as error:
                relay.model_ready(timeout=0.5)
            assert error.value.code == status
        else:
            with pytest.raises(ValueError):
                relay.model_ready(timeout=0.5)
        assert len(requests) == 1
        method, path, authorization, request_id = requests[0]
        assert (method, path, authorization) == ('GET', '/v1/models', 'Bearer private-capability')
        assert UUID(request_id).hex == request_id
        assert relay.last_failure is None and not diagnostics


def test_compaction_waits_for_unbilled_admission_with_fresh_envelopes(monkeypatch):
    requests, envelopes = [], []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            envelope = self.rfile.read(int(self.headers['Content-Length']))
            envelopes.append(envelope)
            requests.append(json.loads(unseal('token', self.path, envelope)))
            self.send_response(429 if len(requests) in {1, 3} else 200)
            if len(requests) == 1:
                self.send_header('X-Moyai-Model-Queue', '1')
            self.end_headers()
            self.wfile.write(b'{"summary":"Complete saved summary"}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'token').start()
    relay.before_model = lambda: pytest.fail('Compaction restarted the stopped runtime')
    monkeypatch.setattr('sandbox.broker_relay.time.sleep', lambda seconds: None)
    entries = [{'seq': 1, 'excerpt': 'Already completed action receipt'}]
    try:
        assert relay.compact('', entries) == 'Complete saved summary'
        assert requests == [{'summary': '', 'entries': entries, 'cursor_protocol': 1}] * 2
        assert envelopes[0] != envelopes[1]
        with pytest.raises(urllib.error.HTTPError) as error:
            relay.compact('', entries)
        assert error.value.code == 429 and len(requests) == 3
    finally:
        relay.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


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
    guidance, *forwarded = received[0]['messages']
    assert guidance['role'] == 'system' and guidance['content'].startswith('MOYAI SKILLS FOR THE CURRENT REQUESTER.')
    assert json.loads(guidance['content'].split('\n', 1)[1]) == {
        'turn_id': run['active_message_id'], 'available': [], 'omitted': 0,
        'matches': [], 'loaded': [], 'unavailable': []}
    assert forwarded == payload['messages']
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


def test_sealed_tool_writes_execute_directly_without_approval(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['slack'])
    calls = []
    async def send(name, arguments, **kwargs):
        calls.append((name,arguments))
        return {'ok': True}
    monkeypatch.setattr(app.state.connectors,'call',send)
    body = {'name': 'slack_send', 'arguments': {'channel': 'C12345678', 'text': CODE}}
    packet = seal('run-capability-only','/tools/call',json.dumps(body).encode())
    response = client.post(f'/broker/{run_id}/tools/call', content=packet,
                           headers={**headers, 'Content-Type': CONTENT_TYPE})
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert calls == [('slack_send', body['arguments'])]
    assert not app.state.store.approvals(run_id)
    assert app.state.store.run(run_id)['status'] == 'running'


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
    expired=[False]
    request_id='a'*32
    class Edge(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            path=self.path.removeprefix('/broker/run')
            body=json.loads(unseal('runtime-token',path,self.rfile.read(int(self.headers['Content-Length']))))
            calls.append((path,body))
            result={'moyai_wait_credential':request_id} if path in {'/tools/call','/credentials/materialize'} or expired[0] else {'data':[]}
            self.send_response(401 if expired[0] else 200);self.send_header('Content-Type','application/json');self.end_headers()
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
        with httpx.Client(base_url=relay.url,timeout=5) as client:
            for name in ['credentials_report_failure','credentials_http_request']:
                relay.wait_credential=''
                client.post('/tools/call',json={'name':name,'arguments':{}},headers=headers)
                assert relay.wait_credential==request_id
            relay.wait_credential=''
            client.post('/credentials/materialize',json={'request_ids':[request_id]},headers=headers)
            assert relay.wait_credential==request_id
            expired[0]=True
            relay.wait_credential=''
            assert client.get(base+'/models',headers=headers).status_code==401
            assert relay.wait_credential==request_id
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


def test_large_tool_call_crosses_loopback_relay_without_raising_model_limit():
    received = []
    payload = {'name': 'github_create_pull_request', 'arguments': {'content': 'x' * (6 * 1024 * 1024)}}
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            received.append(json.loads(unseal('runtime-token', self.path, raw)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', 'runtime-token').start()
    try:
        with httpx.Client(base_url=relay.url, timeout=10) as client:
            headers = {'Authorization': 'Bearer runtime-token'}
            assert client.post('/tools/call', json=payload, headers=headers).json() == {'ok': True}
            assert client.post('/v1/chat/completions', json=payload, headers=headers).status_code == 413
        assert received == [payload]
    finally:
        relay.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
