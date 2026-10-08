import asyncio
from copy import deepcopy
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
from sandbox.transport_recovery import recovery_marker, retryable_failure, validate_recovery


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


@pytest.mark.parametrize('unsafe', ['', 'pending', 'stopped', 'partial', 'completed', 'tool_transport',
    'stream', 'permanent', 'boundary_failed', 'missing_store', 'different_store'])
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
    if unsafe == 'boundary_failed': agent.boundary_failed = True
    if unsafe == 'missing_store': agent.context_store = None
    if unsafe == 'different_store': agent.journal.context_store = object()
    try:
        # A live native thread can retain its tools through an eligible model
        # failure; that does not authorize restoring unknown tools into a new one.
        if unsafe == 'pending':
            assert retryable_failure(failure)
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


@pytest.fixture(params=['/v1/chat/completions', '/v1/messages', '/v1/responses'])
async def live_owner(request, monkeypatch):
    from app import live_context
    from app.context_budget import Budget, ContextPressure
    from app.model_slots import ModelSlots
    route = request.param
    proof = SimpleNamespace(route=route, field='input' if route == '/v1/responses' else 'messages',
        calls=[], queue=asyncio.Queue(), limit=20000, clock=1000., slots=ModelSlots(1))
    monkeypatch.setattr(live_context, 'time', SimpleNamespace(monotonic=lambda: proof.clock))
    proof.run = dict(id='run', token_hash='cap', active_message_id=1, active_user_id='actor',
                     active_model='model', model='model', status='running', deleted_at='', mode='modal')
    proof.runs = {'run': proof.run}
    def message(role, text):
        if route == '/v1/chat/completions': return {'role': role, 'content': text}
        kind = ('output_text' if role == 'assistant' else 'input_text') if route == '/v1/responses' else 'text'
        return {'role': role, 'content': [{'type': kind, 'text': text}]}
    proof.message = message
    proof.items = [message('user', 'Original request: preserve every receipt')] + [
        message('assistant', f'receipt-{index}: ' + 'x' * 1050) for index in range(14)]
    proof.payload = {'model': 'model', proof.field: proof.items, 'tools': [],
                     'system' if route == '/v1/messages' else 'instructions': 'Keep the platform constraints'}
    async def measure(payload, **kwargs):
        return Budget(len(live_context.encoded(payload).encode()), proof.limit, 1000, proof.limit + 1000, 'fixture_bytes')
    async def check(payload, **kwargs):
        value = await measure(payload)
        if value.input_tokens > value.input_budget: raise ContextPressure(value.public())
        return value
    async def summarize(run, req, history, model, size):
        async with proof.slots:
            call = SimpleNamespace(history=deepcopy(history), release=asyncio.Event(), fail=False,
                task=asyncio.current_task(), summary=f'private-working-summary-{len(proof.calls)}')
            proof.calls.append(call)
            proof.queue.put_nowait(call)
            await call.release.wait()
            if call.fail: raise RuntimeError('private-provider-error')
            return call.summary
    gateway = SimpleNamespace(model_slots=proof.slots, context_budget=SimpleNamespace(measure=measure, check=check),
        summarize_private=summarize, require_run=lambda run_id, *args: dict(proof.runs.get(run_id, proof.run)),
        store=SimpleNamespace(run=lambda run_id: dict(proof.runs.get(run_id, proof.run))),
        settings=SimpleNamespace(resolve_model=lambda fallback: fallback))
    proof.owner = live_context.LiveContext(gateway)
    async def prepare(items=None, run=None):
        current = proof.run if run is None else run
        proof.runs[current['id']] = current
        return await proof.owner.prepare(dict(current), None,
            {**proof.payload, proof.field: proof.items if items is None else items}, route)
    def block_measure(run_id=None):
        gates = asyncio.Queue()
        async def blocked(payload, **kwargs):
            if run_id is None or kwargs.get('scope') == run_id + route:
                gate = asyncio.get_running_loop().create_future()
                gates.put_nowait(gate)
                await gate
            return await measure(payload, **kwargs)
        gateway.context_budget.measure = blocked
        return gates
    async def next_call(): return await asyncio.wait_for(proof.queue.get(), 2)
    async def settle(call):
        call.release.set()
        await asyncio.wait_for(asyncio.shield(call.task), 2)
    proof.prepare, proof.next, proof.settle = prepare, next_call, settle
    proof.block_measure = block_measure
    yield proof
    await proof.owner.close()


async def test_live_summary_starts_at_seventy_five_percent(live_owner):
    from app.live_context import encoded
    p = live_owner
    items = p.items[:-2] + [p.message('assistant', '')]
    padding = 14999 - len(encoded({**p.payload, p.field: items}).encode())
    assert padding > 0
    items[-1] = p.message('assistant', 'x' * padding)
    body, budget = await p.prepare(items)
    assert budget.input_tokens == 14999 and body[p.field] == items
    assert not p.owner.tasks and not p.calls
    items[-1] = p.message('assistant', 'x' * (padding + 1))
    _, budget = await p.prepare(items)
    assert budget.input_tokens == 15000
    assert (await p.next()).history and len(p.owner.tasks) == 1


async def test_live_prefix_runs_ahead_keeps_tail_and_compacts_again(live_owner):
    p = live_owner
    original = deepcopy(p.payload)
    assert (await p.prepare())[0] == original
    first = await p.next()
    tail = [p.message('assistant', 'new receipt'), p.message('user', 'Newest correction: do not deploy')]
    for count in (1, 2):
        pending, _ = await p.prepare(p.items + tail[:count])
        assert pending[p.field] == p.items + tail[:count]
    assert len(p.calls) == 1 and not first.release.is_set()
    await p.settle(first)
    projected, _ = await p.prepare(p.items + tail)
    assert projected[p.field][-4:] == p.items[-2:] + tail
    assert first.summary in json.dumps(projected) and p.payload == original
    more = [p.message('assistant', f'newer-{index} ' + 'y' * 1050) for index in range(6)]
    extended = p.items + tail + more
    await p.prepare(extended)
    second = await p.next()
    assert first.summary in json.dumps(second.history)
    assert not set(first.history).intersection(second.history)
    await p.settle(second)
    final, _ = await p.prepare(extended)
    assert final[p.field][-2:] == more[-2:]
    assert second.summary in json.dumps(final) and first.summary not in json.dumps(final)
    assert {key: value for key, value in final.items() if key != p.field} == {
        key: value for key, value in original.items() if key != p.field}


@pytest.mark.parametrize('anchor_kind', ['correction', 'opaque'])
async def test_live_repeated_compaction_preserves_work_order_across_retained_anchor(live_owner, anchor_kind):
    p = live_owner
    anchor = p.message('user', 'Latest correction: preserve the new deployment policy')
    if anchor_kind == 'opaque':
        block = ({'type': 'input_image', 'image_url': 'opaque-anchor'} if p.route == '/v1/responses' else
                 {'type': 'image', 'source': {'type': 'url', 'url': 'opaque-anchor'}} if p.route == '/v1/messages' else
                 {'type': 'image_url', 'image_url': {'url': 'opaque-anchor'}})
        anchor = {'role': 'assistant', 'content': [block]}
    later = [p.message('assistant', f'after-anchor-{index}: ' + 'y' * 1050) for index in range(20)]
    items = p.items[:7] + [anchor] + later[:8]
    def assert_order(body):
        position = body.index(anchor)
        for index, item in enumerate(body):
            rendered = json.dumps(item)
            if 'after-anchor-' in rendered: assert index > position
            for call in p.calls:
                if call.summary in rendered:
                    assert (index > position) == ('after-anchor-' in json.dumps(call.history))
    await p.prepare(items)
    first = await p.next()
    await p.settle(first)
    assert_order((await p.prepare(items))[0][p.field])
    items += later[8:14]
    await p.prepare(items)
    for _ in range(2):
        await p.settle(await p.next())
        assert_order((await p.prepare(items))[0][p.field])
    assert 'after-anchor-' in json.dumps(p.calls[-1].history)
    items += later[14:]
    await p.prepare(items)
    await p.settle(await p.next())
    final = (await p.prepare(items))[0][p.field]
    assert_order(final)
    assert final[0] == p.items[0] and final[-2:] == later[-2:]


async def test_live_concurrent_branches_and_native_reset_do_not_share_unmatched_prefix(live_owner):
    p = live_owner
    branch = [p.message('user', 'Independent native conversation'), *p.items[1:]]
    await p.prepare()
    first = await p.next()
    await p.prepare(branch)
    await p.settle(first)
    second = await p.next()
    assert (await p.prepare(branch))[0][p.field] == branch
    await p.settle(second)
    for items, expected, excluded in [(p.items, first.summary, second.summary), (branch, second.summary, first.summary)]:
        body = (await p.prepare(items))[0]
        assert expected in json.dumps(body) and excluded not in json.dumps(body)
    reset = [p.message('user', 'Fresh runtime request'), p.message('assistant', 'Starting fresh')]
    assert (await p.prepare(reset))[0][p.field] == reset


@pytest.mark.parametrize('kind', ['closed', 'pending', 'opaque'])
async def test_live_prefix_keeps_tool_groups_media_and_private_reasoning(live_owner, kind):
    p = live_owner
    if p.route == '/v1/responses':
        group = [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'private-reasoning-marker'}]},
                 {'type': 'function_call', 'call_id': 'native-tool', 'name': 'read', 'arguments': '{}'}]
        result = {'type': 'function_call_output', 'call_id': 'native-tool',
                  'output': [{'type': 'input_text', 'text': 'confirmed-tool-result'}]}
        if kind == 'opaque': result['output'] = [{'type': 'input_image', 'image_url': 'opaque-media-marker'}]
    elif p.route == '/v1/messages':
        group = [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'private-reasoning-marker'},
                  {'type': 'tool_use', 'id': 'native-tool', 'name': 'read', 'input': {}}]}]
        result = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'native-tool',
            'content': [{'type': 'image', 'source': {'type': 'url', 'url': 'opaque-media-marker'}}]
                       if kind == 'opaque' else 'confirmed-tool-result'}]}
    else:
        group = [{'role': 'assistant', 'reasoning_content': 'private-reasoning-marker', 'content': None,
                  'tool_calls': [{'id': 'native-tool', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]}]
        result = {'role': 'tool', 'tool_call_id': 'native-tool', 'content':
            [{'type': 'image_url', 'image_url': {'url': 'opaque-media-marker'}}] if kind == 'opaque' else 'confirmed-tool-result'}
    if kind != 'pending': group.append(result)
    items = p.items[:4] + group + p.items[4:]
    await p.prepare(items)
    call = await p.next()
    assert 'private-reasoning-marker' not in json.dumps(call.history)
    assert ('native-tool' in json.dumps(call.history)) == (kind == 'closed')
    await p.settle(call)
    body = (await p.prepare(items))[0][p.field]
    if kind == 'closed':
        assert 'native-tool' not in json.dumps(body)
    elif kind == 'pending':
        assert body[-len(items[4:]):] == items[4:]
    else:
        assert all(item in body for item in group)
        assert 'opaque-media-marker' not in json.dumps(call.history)


async def test_live_failed_summary_can_retry_and_close_cancels_owned_work(live_owner):
    from app.live_context import RETRY_SECONDS
    p = live_owner
    await p.prepare()
    state = next(iter(p.owner.states.values()))
    first = await p.next()
    first.fail = True
    await p.settle(first)
    assert (await p.prepare())[0][p.field] == p.items and len(p.calls) == 1
    p.clock += RETRY_SECONDS + 1
    await p.prepare()
    retry = await p.next()
    assert retry.history == first.history and next(iter(p.owner.states.values())) is state
    await p.owner.close()
    assert retry.task.cancelled() and not p.owner.states
    assert (await p.prepare())[0][p.field] == p.items


async def test_live_delayed_completion_cannot_remove_replacement_for_the_same_prefix(live_owner):
    from app.live_context import RETRY_SECONDS
    p, delayed = live_owner, []
    class HeldCallbackTask(asyncio.Task):
        def add_done_callback(self, callback, *, context=None):
            if getattr(callback, '__qualname__', '').startswith('LiveContext.prepare.<locals>.'):
                delayed.append(callback)
            else:
                super().add_done_callback(callback, context=context)
    loop = asyncio.get_running_loop()
    factory = loop.get_task_factory()
    loop.set_task_factory(lambda loop, coroutine, **kwargs: HeldCallbackTask(coroutine, loop=loop, **kwargs))
    try:
        await p.prepare()
    finally:
        loop.set_task_factory(factory)
    first = await p.next()
    state = next(iter(p.owner.states.values()))
    key = next(iter(state.pending))
    first.fail = True
    await p.settle(first)
    p.clock += RETRY_SECONDS + 1
    await p.prepare()
    replacement = await p.next()
    assert len(delayed) == 1 and state.pending[key] is replacement.task
    delayed[0](first.task)
    assert state.pending[key] is replacement.task


@pytest.mark.parametrize('retirement', ['scope', 'idle'])
async def test_live_retirement_cancels_held_summary_without_another_request(live_owner, retirement):
    from app.live_context import IDLE_SECONDS
    p = live_owner
    await p.prepare()
    call = await p.next()
    if retirement == 'scope': p.run['token_hash'] = 'replacement-capability'
    else: p.clock += IDLE_SECONDS + 1
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(call.task), 2)
    await p.owner.close()
    assert not p.owner.states and not p.owner.tasks and not call.release.is_set()


@pytest.mark.parametrize('change', ['token_hash', 'active_message_id', 'active_user_id', 'active_model'])
async def test_live_measurement_rechecks_scope_and_discards_stale_completion(live_owner, change):
    from fastapi import HTTPException
    p = live_owner
    await p.prepare()
    call = await p.next()
    measure = p.owner.gateway.context_budget.measure
    async def change_scope(payload, **kwargs):
        p.run[change] = 2 if change == 'active_message_id' else 'different-scope'
        await asyncio.sleep(0)
        return await measure(payload, **kwargs)
    p.owner.gateway.context_budget.measure = change_scope
    with pytest.raises(HTTPException) as rejected:
        await p.prepare()
    assert rejected.value.status_code == 409
    await p.settle(call)
    assert all(not state.projections and not state.pending for state in p.owner.states.values())


async def test_live_global_caps_keep_evicted_tasks_owned_until_cleanup(live_owner, monkeypatch):
    from app import live_context
    p = live_owner
    for name in ('MAX_SCOPES', 'MAX_TASKS', 'MAX_CACHE_BYTES'):
        monkeypatch.setattr(live_context, name, 1)
    await p.prepare()
    first = await p.next()
    p.run['id'] = 'another-run'
    await p.prepare()
    assert first.task.cancelling() and first.task in p.owner.tasks
    assert len(p.owner.states) == 1 and not next(iter(p.owner.states.values())).pending
    await asyncio.gather(first.task, return_exceptions=True)
    assert not p.owner.tasks
    await p.prepare()
    second = await p.next()
    second.release.set()
    await asyncio.gather(second.task, return_exceptions=True)
    assert len(p.owner.states) == 1 and not p.owner.tasks
    assert all(not state.projections and state.rejected for state in p.owner.states.values())


@pytest.mark.parametrize('saturated', [False, True])
async def test_live_hard_limit_waits_without_holding_the_single_model_slot(live_owner, saturated):
    p = live_owner
    await p.prepare()
    first = await p.next()
    if saturated:
        await p.prepare([p.message('user', 'Second independent task'), *p.items[1:]])
    extra = [p.message('assistant', 'tail ' + 'z' * 1050) for _ in range(8)]
    items = ([p.message('user', 'Third independent task'), *p.items[1:]] if saturated else p.items) + extra
    waiting = asyncio.create_task(p.prepare(items))
    await asyncio.sleep(0)
    assert not waiting.done()
    await p.settle(first)
    if saturated:
        await p.settle(await p.next())
        await p.settle(await p.next())
    result, budget = await asyncio.wait_for(waiting, 2)
    assert budget.input_tokens <= budget.input_budget and result[p.field][-len(extra):] == extra


async def test_live_exhausted_capacity_retries_report_context_pressure(live_owner, monkeypatch):
    from app import live_context
    from app.context_budget import ContextPressure
    p, measured = live_owner, []
    monkeypatch.setattr(live_context, 'MAX_TASKS', 1)
    measure = p.owner.gateway.context_budget.measure
    async def saturated(payload, **kwargs):
        budget = await measure(payload, **kwargs)
        measured.append(budget)
        task = p.slots.run_maintenance(asyncio.sleep(0))
        p.owner.tasks.add(task)
        task.add_done_callback(p.owner.tasks.discard)
        return budget
    p.owner.gateway.context_budget.measure = saturated
    with pytest.raises(ContextPressure) as rejected:
        await p.prepare(p.items + p.items[1:9])
    assert len(measured) == 4 and rejected.value.budget == measured[-1].public()
    assert not p.calls and not p.owner.tasks


@pytest.mark.parametrize('sweep', ['watch', 'admission'])
async def test_live_borrowed_scope_survives_idle_sweeps_and_gets_release_grace(live_owner, sweep):
    from app.live_context import IDLE_SECONDS
    p = live_owner
    await p.prepare()
    summary = await p.next()
    state = p.owner.states[(p.run['id'], p.route)]
    waiting = asyncio.create_task(p.prepare(p.items + p.items[1:9]))
    try:
        await asyncio.sleep(0)
        p.clock += IDLE_SECONDS + 1
        if sweep == 'watch': await asyncio.sleep(1.05)
        else: await p.prepare([p.message('user', 'Another foreground request')])
        assert p.owner.states[(p.run['id'], p.route)] is state and not summary.task.done()
        await p.settle(summary)
        body, budget = await asyncio.wait_for(waiting, 2)
        assert summary.summary in json.dumps(body) and budget.input_tokens <= budget.input_budget
        p.clock += IDLE_SECONDS - 1
        await p.prepare([p.message('user', 'Next request within the renewed grace')])
        assert p.owner.states[(p.run['id'], p.route)] is state
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_live_concurrent_borrowers_release_on_success_cancel_and_error(live_owner):
    from app.live_context import IDLE_SECONDS
    p = live_owner
    gates = p.block_measure()
    tasks = [asyncio.create_task(p.prepare([p.message('user', 'Small foreground request')])) for _ in range(3)]
    try:
        held = [await asyncio.wait_for(gates.get(), 2) for _ in tasks]
        state = p.owner.states[(p.run['id'], p.route)]
        assert state.borrowers == 3
        p.clock += IDLE_SECONDS + 1
        held[0].set_result(None)
        await tasks[0]
        assert state.borrowers == 2 and not state.idle(p.clock + IDLE_SECONDS + 1)
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        assert state.borrowers == 1
        p.clock += 1
        held[2].set_exception(RuntimeError('counter unavailable'))
        with pytest.raises(RuntimeError, match='counter unavailable'): await tasks[2]
        assert state.borrowers == 0 and not state.idle(p.clock + IDLE_SECONDS - 1)
        assert state.idle(p.clock + IDLE_SECONDS + 1)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('bound', ['MAX_SCOPES', 'MAX_CACHE_BYTES'])
async def test_live_capacity_preserves_borrowed_scope_and_existing_projection(live_owner, monkeypatch, bound):
    from app import live_context
    from app.context_budget import ContextPressure
    p = live_owner
    await p.prepare()
    saved = await p.next()
    await p.settle(saved)
    key = (p.run['id'], p.route)
    state = p.owner.states[key]
    projections = list(state.projections)
    monkeypatch.setattr(live_context, bound, 1 if bound == 'MAX_SCOPES' else sum(v.size_bytes for v in projections))
    gates = p.block_measure(p.run['id'])
    waiting = asyncio.create_task(p.prepare())
    gate = await asyncio.wait_for(gates.get(), 2)
    other = {**p.run, 'id': 'another-run'}
    try:
        body, _ = await p.prepare(run=other)
        assert body[p.field] == p.items
        if bound == 'MAX_SCOPES':
            with pytest.raises(ContextPressure):
                await asyncio.wait_for(p.prepare(p.items + p.items[1:9], run=other), 2)
            assert list(p.owner.states) == [key]
        else:
            empty = {**p.run, 'id': 'empty-cache-run'}
            await p.prepare([p.message('user', 'No cache bytes to evict')], run=empty)
            await p.settle(await p.next())
            assert not p.owner.states[(other['id'], p.route)].projections
            assert (empty['id'], p.route) in p.owner.states  # Failed admission evicts nothing.
        assert p.owner.states[key] is state and state.projections == projections
        gate.set_result(None)
        assert saved.summary in json.dumps((await asyncio.wait_for(waiting, 2))[0])
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_live_scope_retirement_wakes_waiter_on_unrelated_capacity(live_owner, monkeypatch):
    from fastapi import HTTPException
    from app import live_context
    p = live_owner
    monkeypatch.setattr(live_context, 'MAX_TASKS', 1)
    await p.prepare()
    unrelated = await p.next()
    other = {**p.run, 'id': 'waiting-run'}
    waiting = asyncio.create_task(p.prepare(p.items + p.items[1:9], run=other))
    try:
        await asyncio.sleep(0)
        other['token_hash'] = 'revoked-capability'
        with pytest.raises(HTTPException): await asyncio.wait_for(waiting, 2)
        assert not unrelated.task.done() and len(p.calls) == 1
        assert (other['id'], p.route) not in p.owner.states
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_live_close_during_measurement_prevents_orphan_continuation(live_owner):
    from fastapi import HTTPException
    p = live_owner
    gates = p.block_measure()
    waiting = asyncio.create_task(p.prepare())
    try:
        gate = await asyncio.wait_for(gates.get(), 2)
        state = p.owner.states[(p.run['id'], p.route)]
        await p.owner.close()
        gate.set_result(None)
        with pytest.raises(HTTPException): await asyncio.wait_for(waiting, 2)
        assert state.borrowers == 0 and not p.calls and not p.owner.states and not p.owner.tasks
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_live_cancelling_one_borrower_preserves_shared_summary_and_other_waiter(live_owner):
    p = live_owner
    await p.prepare()
    summary = await p.next()
    baseline = asyncio.all_tasks()
    tasks = [asyncio.create_task(p.prepare(p.items + p.items[1:9])) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert not summary.task.done() and not tasks[1].done()
        await p.settle(summary)
        body, budget = await asyncio.wait_for(tasks[1], 2)
        assert summary.summary in json.dumps(body) and budget.input_tokens <= budget.input_budget
        assert not asyncio.all_tasks() - baseline - p.owner.tasks
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_live_cache_admission_accounts_for_replaced_projection_bytes(live_owner, monkeypatch):
    from app import live_context
    p = live_owner
    await p.prepare()
    first = await p.next()
    await p.settle(first)
    state = p.owner.states[(p.run['id'], p.route)]
    limit = state.projections[0].size_bytes
    monkeypatch.setattr(live_context, 'MAX_PREFIXES', 1)
    monkeypatch.setattr(live_context, 'MAX_CACHE_BYTES', limit)
    extended = p.items + [p.message('assistant', 'New tail ' + 'y' * 1050) for _ in range(6)]
    await p.prepare(extended)
    second = await p.next()
    await p.settle(second)
    body = (await p.prepare(extended))[0]
    assert len(state.projections) == 1 and state.projections[0].size_bytes <= limit
    assert second.summary in json.dumps(body) and first.summary not in json.dumps(body)
