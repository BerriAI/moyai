import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from sandbox.broker_relay import BrokerRelay
from sandbox.context_recovery import run_with_context_recovery
from sandbox.context_store import ContextStore, ContextUnavailable
from sandbox.harness_agent import TurnJournal
from sandbox.transport_recovery import recovery_marker, validate_recovery


def runtime(tmp_path):
    store = ContextStore(tmp_path / 'context.db', 'run')
    store.initialize([])
    summaries = []
    def compact(summary, entries, **kwargs):
        summaries.append(entries)
        return {'summary': 'Do not deploy. Completed receipts remain in the journal.',
                'through_seq': entries[-1]['seq']}
    relay = SimpleNamespace(context_required=None, compact=compact)
    agent = SimpleNamespace(context=SimpleNamespace(relay=relay, spec={}, cwd=str(tmp_path),
        activity=SimpleNamespace(commentary=lambda text: None)), context_store=store,
        stopped=threading.Event(), pending_text=[], journal=TurnJournal([], 'Do not deploy.', store))
    return agent, summaries


@pytest.mark.parametrize('unsafe', ['', 'pending', 'stopped', 'partial', 'completed', 'tool_transport', 'stream', 'permanent', 'missing_store'])
def test_transport_recovery_requires_settled_durable_receipts(tmp_path, unsafe):
    agent, _ = runtime(tmp_path)
    store = agent.context_store
    failure = {'version': 1, 'route': '/v1/messages', 'http_status': 502, 'request_id': 'edge-fixture',
               'response_started': False, 'transient': True}
    agent.context.relay.last_failure = failure
    result = {'failed': True}
    agent.journal.tool_started('write-one', 'publish', {})
    if unsafe != 'pending':
        agent.journal.tool_finished('write-one', 'Published once: receipt-one')
    if unsafe == 'stopped': agent.stopped.set()
    if unsafe in {'partial', 'completed'}: result[unsafe] = True
    if unsafe == 'tool_transport': agent.context.relay.uncertain_tool = True
    if unsafe == 'stream': failure['response_started'] = True
    if unsafe == 'permanent': failure['transient'] = False
    if unsafe == 'missing_store': agent.context_store = None
    try:
        marker = recovery_marker(agent, result)
        assert bool(marker) is (unsafe == '')
        if marker:
            validate_recovery(store, marker)
            assert 'receipt-one' in store.history()[0]['content']
            store.append({'role': 'user', 'content': 'A newer checkpoint'})
            with pytest.raises(ContextUnavailable, match='checkpoint'):
                validate_recovery(store, marker)
    finally:
        store.close()


def test_long_task_can_compact_repeatedly_without_replaying_tools(tmp_path):
    agent, summaries = runtime(tmp_path)
    calls = []
    prompts = []
    def invoke(prompt):
        prompts.append(prompt)
        assert 'Do not deploy.' in prompt
        assert len(prompt.encode()) < 16000
        if len(calls) == 30:
            return {'completed': True, 'final_response': 'done'}
        index = len(calls)
        agent.journal.tool_started(str(index), 'write', {'number': index})
        calls.append(index)
        agent.journal.tool_finished(str(index), 'receipt ' + str(index) + 'x' * 25000)
        agent.context.relay.context_required = {'input_tokens': 30000, 'input_budget': 16000}
        return {'failed': True}
    try:
        assert run_with_context_recovery(agent, 'Do not deploy.', [], invoke)['completed']
        assert calls == list(range(30))
        assert len(summaries) >= 30 and len(prompts) == 31
        assert agent.context_store.state()['cursor'] == 61
        # No duplicated current requests or synthetic recovery errors in receipts.
        rows = agent.context_store.db.execute('SELECT message FROM journal').fetchall()
        assert len(rows) == 61 and sum(json.loads(r[0])['role'] == 'user' for r in rows) == 1
    finally:
        agent.context_store.close()


def test_immutable_oversized_input_stops_when_rebuild_does_not_reduce_request(tmp_path):
    agent, summaries = runtime(tmp_path)
    attempts = []
    def invoke(prompt):
        attempts.append(1)
        agent.pending_text.append('SDK ERROR: prompt too long')
        agent.context.relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
        raise RuntimeError('SDK rejected context')
    try:
        with pytest.raises(ContextUnavailable, match='still exceed'):
            run_with_context_recovery(agent, 'Required input ' * 2000, [], invoke)
        assert len(attempts) == 2 and len(summaries) == 1
        assert agent.context_store.state()['cursor'] == 1
        assert 'SDK ERROR' not in agent.context_store.history()[0]['content']
    finally:
        agent.context_store.close()


@pytest.mark.parametrize('pending,stopped', [(True, False), (False, True)])
def test_pending_tool_or_user_stop_blocks_automatic_restart(tmp_path, pending, stopped):
    agent, summaries = runtime(tmp_path)
    def invoke(prompt):
        if pending:
            agent.journal.tool_started('unknown', 'publish', {})
        if stopped:
            agent.stopped.set()
        agent.context.relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
        return {'interrupted': True}
    try:
        if pending:
            with pytest.raises(ContextUnavailable, match='pending'):
                run_with_context_recovery(agent, 'task', [], invoke)
        else:
            assert run_with_context_recovery(agent, 'task', [], invoke)['interrupted']
        assert summaries == []
    finally:
        agent.context_store.close()


def test_failed_compaction_keeps_cursor_and_receipts_across_cold_restore(tmp_path):
    agent, _ = runtime(tmp_path)
    agent.journal.tool_started('one', 'write', {})
    agent.journal.tool_finished('one', 'durable receipt')
    def interrupted(summary, entries, **kwargs):
        raise RuntimeError('process failed before commit')
    try:
        with pytest.raises(ContextUnavailable):
            agent.context_store.compact(interrupted, force=True)
    finally:
        agent.context_store.close()
    restored = ContextStore(tmp_path / 'context.db', 'run')
    assert restored.state()['cursor'] == 0 and not restored.pending
    assert 'durable receipt' in restored.history()[0]['content']
    restored.close()


@pytest.mark.parametrize('mode', ['fresh-session-adapter', 'hermes-native', 'codex-compact'])
@pytest.mark.parametrize('route', ['/v1/messages', '/v1/responses', '/v1/chat/completions'])
def test_relay_preserves_context_signal_and_never_replays_rejected_request(mode, route):
    seen, boundaries = [], []
    allowed, upstream_failure = True, False
    managed = mode != 'hermes-native'
    native_compact = mode == 'codex-compact' and route == '/v1/responses'
    def before_model(raw):
        boundaries.append(raw)
        return allowed
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers['Content-Length'])))
            raw = json.dumps({'error': {'message': 'Temporary fixture failure'}} if upstream_failure else
                {'detail': {'code': 'context_compaction_required',
                    'input_tokens': 20000, 'input_budget': 10000}}).encode()
            self.send_response(503 if upstream_failure else 409)
            if not upstream_failure:
                self.send_header('X-Moyai-Context', 'compact')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{edge.server_port}/broker/run', 'cap').start()
    relay.context_recovery = managed
    relay.native_compacting = mode == 'codex-compact'
    relay.before_model = before_model
    try:
        with httpx.Client(base_url=relay.url, headers={'Authorization': 'Bearer cap'}) as client:
            response = client.post(route, json={'messages': []})
            assert response.status_code == (200 if native_compact else 400)
            if native_compact:
                assert response.headers['Content-Type'] == 'text/event-stream'
                event = json.loads(response.text.removeprefix('data: '))
                assert event['type'] == 'response.failed'
                assert event['response']['error']['code'] == 'context_length_exceeded'
            else:
                assert response.json()['error']['code'] == 'context_length_exceeded'
            assert bool(relay.context_required) == managed
            assert not relay.last_error and not relay.model_failed and relay.last_failure is None
            assert len(seen) == len(boundaries) == 1
            # Only an explicit native Responses compact may pass the pressure
            # latch; Hermes retains its existing ownership without a latch.
            client.post(route, json={'messages': []})
            assert len(seen) == len(boundaries) == (2 if native_compact or not managed else 1)
            assert bool(relay.context_required) == managed
            if native_compact:
                allowed = False
                assert client.post(route, json={}).status_code == 409
                assert len(seen) == 2 and len(boundaries) == 3
                allowed, relay.model_failed = True, True
                response = client.post(route, json={})
                assert response.status_code == 409
                assert response.json()['error']['code'] == 'broker_recovery_required'
                assert len(seen) == 2 and len(boundaries) == 3
                relay.model_failed, relay.native_compacting = False, False
                assert client.post(route, json={}).status_code == 400
                assert len(seen) == 2 and len(boundaries) == 3
                relay.native_compacting, upstream_failure = True, True
                response = client.post(route, json={})
                assert response.status_code == 503
                assert response.headers['Content-Type'] == 'application/json'
                assert response.json()['error']['code'] == 'broker_error'
                assert relay.model_failed and len(seen) == 3 and len(boundaries) == 4
    finally:
        relay.close()
        edge.shutdown()
        edge.server_close()


@pytest.mark.parametrize('route', ['/v1/messages', '/v1/responses', '/v1/chat/completions'])
@pytest.mark.parametrize('native_compacting', [False, True])
def test_full_history_at_transport_ceiling_requests_recovery_before_inference(route, native_compacting):
    import http.client
    from sandbox.broker_transport import MAX_BODY
    relay = BrokerRelay('http://127.0.0.1:1/unreachable', 'cap').start()
    relay.context_recovery = True
    relay.native_compacting = native_compacting
    connection = http.client.HTTPConnection('127.0.0.1', relay.server.server_port)
    try:
        # The declared length is rejected before reading or forwarding a body.
        connection.request('POST', route, headers={'Authorization': 'Bearer cap', 'Content-Length': str(MAX_BODY + 1)})
        response = connection.getresponse()
        native_compact = native_compacting and route == '/v1/responses'
        assert response.status == (200 if native_compact else 400)
        body = response.read().decode()
        if native_compact:
            assert response.headers['Content-Type'] == 'text/event-stream'
            event = json.loads(body.removeprefix('data: '))
            assert event['type'] == 'response.failed'
            assert event['response']['error']['code'] == 'context_length_exceeded'
        else:
            assert json.loads(body)['error']['code'] == 'context_length_exceeded'
        assert relay.context_required == {'input_tokens': MAX_BODY + 1, 'input_budget': MAX_BODY}
        assert not relay.last_error and not relay.model_failed and relay.last_failure is None
    finally:
        connection.close()
        relay.close()


@pytest.mark.parametrize('disconnect', ['headers', 'body'])
def test_abandoned_compaction_error_does_not_poison_retry(monkeypatch, disconnect):
    seen, failures, writes = [], [], []
    finished = threading.Event()

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            seen.append(self.rfile.read(int(self.headers['Content-Length'])))
            body = json.dumps({'detail': {'code': 'context_compaction_required',
                'input_tokens': 20000, 'input_budget': 10000}}).encode()
            self.send_response(409)
            self.send_header('X-Moyai-Context', 'compact')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{edge.server_port}', 'cap', report_error=failures.append).start()
    relay.context_recovery = relay.native_compacting = True
    handler = relay.server.RequestHandlerClass
    setup, finish = handler.setup, handler.finish

    def broken_setup(self):
        setup(self)
        write = self.wfile.write
        def broken_write(data):
            prefix = b'HTTP/' if disconnect == 'headers' else b'data: '
            if not writes and data.startswith(prefix):
                writes.append(disconnect)
                raise BrokenPipeError('Local SDK abandoned overflow response')
            return write(data)
        self.wfile.write = broken_write

    def observed_finish(self):
        try:
            finish(self)
        finally:
            finished.set()

    monkeypatch.setattr(handler, 'setup', broken_setup)
    monkeypatch.setattr(handler, 'finish', observed_finish)
    try:
        with httpx.Client(base_url=relay.url, headers={'Authorization': 'Bearer cap'}, timeout=3) as client:
            try:
                client.post('/v1/responses', json={'input': []})
            except httpx.HTTPError:
                pass  # Delivery is deliberately broken; inspect the saved state.
            assert finished.wait(3) and writes == [disconnect]
            assert relay.context_required and not relay.model_failed
            assert not relay.last_error and relay.last_failure is None and not failures
            retry = client.post('/v1/responses', json={'input': []})
            assert len(seen) == 2 and retry.status_code == 200
            assert json.loads(retry.text.removeprefix('data: '))['response']['error']['code'] == 'context_length_exceeded'
            assert not relay.model_failed
    finally:
        relay.close()
        edge.shutdown()
        edge.server_close()


def test_old_unknown_outcome_does_not_block_new_question_recovery(tmp_path):
    agent, summaries = runtime(tmp_path)
    store = agent.context_store
    store.append({'role': 'assistant', 'tool_calls': [{'id': 'old-runtime:write', 'function': {'name': 'publish'}}]})
    calls = []
    def invoke(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            agent.context.relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
            return {'failed': True}
        assert 'UNRESOLVED TOOL OUTCOMES' in prompt and 'old-runtime:write' in prompt
        return {'completed': True, 'final_response': 'That earlier publish has an unknown outcome.'}
    try:
        result = run_with_context_recovery(agent, 'What happened earlier?', store.history(), invoke)
        assert result['completed'] and len(summaries) == 1
        assert store.pending == {'old-runtime:write'} and not agent.journal.pending
    finally:
        store.close()


@pytest.mark.parametrize('harness', ['codex', 'claude-agent-sdk'])
@pytest.mark.parametrize('final_failure', [False, True])
def test_native_context_handoff_reports_only_terminal_failure(tmp_path, monkeypatch, harness, final_failure):
    from sandbox.activity import ActivityReporter
    from sandbox.harness_registry import create_agent
    from test_codex_sdk import install_codex_client, sdk_event
    from claude_agent_sdk import ResultMessage
    original, summaries = runtime(tmp_path)
    store, relay = original.context_store, original.context.relay
    relay.url = 'http://127.0.0.1:1234'
    events, attempts = [], []
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'fixture-capability')
    agent = create_agent(harness, spec={'model': 'openai/gpt-6-astra' if harness == 'codex' else 'anthropic/claude-opus-5-5'},
        relay=relay, config={'mcp_servers': {'workspace': {'command': 'python', 'args': []}}},
        activity=ActivityReporter(lambda *args: events.append(args)), step=lambda: None,
        cwd=str(tmp_path), context_store=store)
    monkeypatch.setattr(agent, 'validate', lambda: None)

    def begin():
        attempts.append(True)
        if len(attempts) == 1:
            relay.context_required = {'input_tokens': 20000, 'input_budget': 10000}
        return len(attempts) == 1 or final_failure

    async def codex_stream():
        failed = begin()
        yield sdk_event('turn/completed', {'turn': {'status': 'failed' if failed else 'completed',
            **({'error': {'codexErrorInfo': 'contextWindowExceeded'}} if failed else {})}})

    class ClaudeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt): pass
        async def receive_messages(self):
            failed = begin()
            yield ResultMessage(subtype='error_during_execution' if failed else 'success',
                duration_ms=1, duration_api_ms=1, is_error=failed, num_turns=1,
                session_id='fixture', result='private-error' if failed else 'done')

    if harness == 'codex':
        install_codex_client(monkeypatch, agent, codex_stream)
    else:
        monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', ClaudeClient)
    try:
        result = agent.run_conversation('Continue.', conversation_history=[], system_message='Moyai')
        assert len(attempts) == 2 and len(summaries) == 1
        assert result['completed'] is not final_failure
        errors = [event for event in events if event[0] == 'error']
        assert len(errors) == int(final_failure)
        assert ('sdk_failure' in result) is final_failure
        assert 'private-error' not in json.dumps([result, events])
    finally:
        agent.close()
        store.close()
