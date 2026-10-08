"""Published Codex runtime, real native/MCP tools, and synthetic Responses inference."""
import json
import re
import shlex
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from sandbox.codex_harness import CodexAgent
from sandbox.context_store import ContextStore


def send_response(handler, output, sequence, input_tokens=500):
    response = {'id': f'resp_fixture_{sequence}', 'status': 'completed', 'output': [output],
                'usage': {'input_tokens': input_tokens, 'output_tokens': 30, 'total_tokens': input_tokens + 30}}
    frames = [
        {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': output},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': output},
        {'type': 'response.completed', 'response': response},
    ]
    raw = ''.join('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n'
                  for frame in frames).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Content-Length', str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


@pytest.mark.parametrize('outcome', [
    'complete', 'interrupt', 'provider-error', 'compaction', 'context-recovery-limit', 'iteration-limit'])
def test_native_astra_tools_checkpoint_and_fresh_context(tmp_path, monkeypatch, outcome):
    requests, attempts, calls, events, faults = [], [], [], [], []
    samples, summaries = [], []
    boundaries, recovered = [], []
    successful = outcome in {'complete', 'compaction', 'context-recovery-limit'}
    max_iterations = {'context-recovery-limit': 4, 'iteration-limit': 2}.get(outcome, 12)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    # A selected workspace must not install its own runtime tools or inject a
    # second set of instructions alongside Moyai's requester-scoped context.
    (workspace / '.codex').mkdir()
    marker = tmp_path / 'untrusted-tool-started'
    (workspace / '.codex' / 'config.toml').write_text(
        '[mcp_servers.untrusted]\ncommand="/bin/sh"\nargs=["-c",'
        + json.dumps('touch ' + str(marker)) + ']\n')
    (workspace / 'AGENTS.md').write_text('untrusted-project-instructions-marker')
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'fixture-capability')
    monkeypatch.setenv('OPENAI_API_KEY', 'inherited-provider-key-marker')
    relay = SimpleNamespace()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != '/tools':
                faults.append('Unexpected GET ' + self.path)
                return self.reply({'error': {'message': 'Unknown route'}}, 404)
            self.reply([{'name': 'echo', 'description': 'Echo synthetic verification data',
                         'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                         'required': ['text'], 'additionalProperties': False}}])

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            data = json.loads(raw)
            if self.headers.get('Authorization') != 'Bearer fixture-capability':
                faults.append('Incorrect run capability')
                return self.reply({'error': {'message': 'Unauthorized'}}, 401)
            if self.path == '/tools/call':
                calls.append(data)
                return self.reply({'text': data['arguments']['text']})
            if self.path != '/v1/responses':
                faults.append('Unexpected POST ' + self.path)
                return self.reply({'error': {'message': 'Unknown route'}}, 404)
            attempts.append(data)
            if getattr(relay, 'context_required', None):
                return self.reply({'error': {'message': 'Waiting for the saved-context handoff',
                    'type': 'context_length_exceeded', 'code': 'context_length_exceeded'}}, 400)
            allowed = relay.before_model(raw)
            boundaries.append({'namespace': agent.journal.call_namespace, 'allowed': allowed,
                'model_calls': agent.model_calls, 'completed_tools': agent.journal.completed_tools,
                'pending': bool(agent.journal.pending)})
            if not allowed:
                return self.reply({'error': {'message': 'Stopped at saved tool boundary'}}, 409)
            requests.append(data)
            if 'You are performing a CONTEXT CHECKPOINT COMPACTION.' in json.dumps(data):
                summaries.append(data)
                output = {'type': 'message', 'id': f'summary_{len(summaries)}', 'role': 'assistant',
                          'status': 'completed', 'content': [{'type': 'output_text',
                              'text': 'native-private-summary-marker. Keep completed tool receipts; continue remaining steps.'}]}
                return send_response(self, output, len(requests), input_tokens=500)
            samples.append(data)
            if outcome == 'provider-error' and len(requests) == 2:
                return self.reply({'error': {'message': 'provider-private-error-marker'}}, 503)
            sequence = len(samples)
            if outcome == 'context-recovery-limit' and sequence == 4:
                relay.context_required = {'input_tokens': 50000, 'input_budget': 20000}
                return self.reply({'error': {'message': 'Context handoff requested',
                    'type': 'context_length_exceeded', 'code': 'context_length_exceeded'}}, 400)
            scripts = [
                'text(await tools.exec_command(' + json.dumps({
                    'cmd': 'test -z "$OPENAI_API_KEY" && printf codex-shell-ok', 'login': False}) + '));',
                'text(await tools.apply_patch(' + json.dumps(
                    '*** Begin Patch\n*** Add File: receipt.txt\n+codex-file-ok\n*** End Patch') + '));',
                'text(await tools.mcp__moyai__echo({text: "codex-mcp-ok"}));',
            ]
            if sequence <= len(scripts):
                output = {'type': 'custom_tool_call', 'id': f'item_{sequence}',
                          'call_id': f'call_{sequence}', 'name': 'exec', 'namespace': 'functions',
                          'input': scripts[sequence - 1]}
            else:
                output = {'type': 'message', 'id': f'msg_{sequence}', 'role': 'assistant',
                          'phase': 'final_answer', 'status': 'completed',
                          'content': [{'type': 'output_text', 'text': 'codex-transport-ok'}]}
            send_response(self, output, len(requests),
                          input_tokens=100000 if outcome == 'compaction' and sequence <= 3 else 500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay.url = f'http://127.0.0.1:{server.server_port}'
    if outcome == 'compaction':
        relay.context_window = lambda: {'input_budget': 35000}
    store = ContextStore(tmp_path / 'context.sqlite3', 'codex-transport')
    store.initialize([])

    def compact(previous, entries, **kwargs):
        recovered.append(entries)
        source = previous + json.dumps(entries)
        return {'summary': 'Completed receipts: ' + ', '.join(marker for marker in (
            'codex-shell-ok', 'codex-file-ok', 'codex-mcp-ok') if marker in source),
            'through_seq': entries[-1]['seq']}

    relay.compact = compact

    def step():
        if outcome == 'interrupt' and agent.journal.completed_tools:
            assert not agent.journal.pending
            agent.interrupt()

    def create_agent():
        return CodexAgent(spec={'model': 'openai/gpt-6-astra', 'timeout': 30, 'max_iterations': max_iterations},
            relay=relay, config={'mcp_servers': {'workspace': {
                'command': sys.executable,
                'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': 'fixture-capability'}}}},
            activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
                complete=lambda *args: events.append(('complete', args)),
                commentary=lambda text: events.append(('commentary', text))),
            step=step, cwd=str(workspace), definition=None, context_store=store)

    agent = create_agent()
    try:
        result = agent.run_conversation('Run the local verification tools once.', conversation_history=[],
                                        system_message='requester-private-first-marker')
        assert not faults
        assert requests and requests[0]['model'] == 'gpt-6-astra'
        serialized = json.dumps(requests)
        assert 'inherited-provider-key-marker' not in serialized
        assert 'untrusted-project-instructions-marker' not in serialized
        assert not marker.exists()
        # Astra's native tool catalog selects code mode. These outer call IDs
        # differ from nested tool receipt IDs. Their outputs must be observed
        # before inference; nested tools must settle before checkpointing.
        catalog = [tool for body in requests for item in body.get('input', [])
                   if item.get('type') == 'additional_tools' for tool in item['tools']]
        assert any(tool.get('name') == 'functions' for tool in catalog)
        assert not any(tool.get('name') == 'collaboration' for tool in catalog)
        started = [args for kind, args in events if kind == 'start']
        completed = [args for kind, args in events if kind == 'complete']
        expected_tools = 3 if successful else 2 if outcome == 'iteration-limit' else 1
        assert len(started) == len(completed) == expected_tools
        assert [args[0] for args in started] == [args[0] for args in completed]
        assert all(args[0] not in {'call_1', 'call_2', 'call_3'} for args in completed)
        assert not agent.journal.pending and not store.pending
        assert 'codex-shell-ok' in json.dumps(result['messages'])
        assert 'provider-private-error-marker' not in json.dumps(result)
        if not successful:
            assert not result['completed']
            assert result['interrupted'] is (outcome == 'interrupt')
            assert result['failed'] is (outcome != 'interrupt')
            assert len(requests) == (1 if outcome == 'interrupt' else 2)
            assert len(attempts) == (3 if outcome == 'iteration-limit' else 2)
            assert not calls
            if outcome == 'provider-error':
                assert result['sdk_failure']['http_status'] == 503
            if outcome == 'iteration-limit':
                assert [boundary['allowed'] for boundary in boundaries] == [True, True, False]
                assert [boundary['model_calls'] for boundary in boundaries] == [1, 2, 2]
                assert len({boundary['namespace'] for boundary in boundaries}) == 1
                assert boundaries[-1]['completed_tools'] == 2 and not boundaries[-1]['pending']
                assert not recovered
                assert (workspace / 'receipt.txt').read_text() == 'codex-file-ok\n'
                assert sum(json.loads(row[0]).get('role') == 'tool'
                           for row in store.db.execute('SELECT message FROM journal')) == 2
                print(f'Native iteration limit: {len(requests)} admitted requests, '
                      f'{len(completed)} settled tools; request {len(attempts)} blocked; '
                      f'{len(recovered)} recovery attempts.')
            return
        assert result['completed'], {'result': result['final_response'], 'boundaries': boundaries,
                                     'recovered': len(recovered)}
        assert result['final_response'] == 'codex-transport-ok'
        assert (workspace / 'receipt.txt').read_text() == 'codex-file-ok\n'
        assert calls == [{'name': 'echo', 'arguments': {'text': 'codex-mcp-ok'}}]
        assert [args[1] for args in completed] == ['terminal', 'apply_patch', 'mcp__moyai__echo']
        if outcome == 'compaction':
            assert len(summaries) >= 2
            assert 'native-private-summary-marker' in json.dumps(samples[-1])
        if outcome == 'context-recovery-limit':
            assert len(recovered) == 1 and store.state()['cursor'] == 7
            assert len(samples) == len(requests) == 5
            assert all(boundary['allowed'] and not boundary['pending'] for boundary in boundaries)
            assert [boundary['model_calls'] for boundary in boundaries] == [1, 2, 3, 4, 1]
            assert len({boundary['namespace'] for boundary in boundaries[:4]}) == 1
            assert boundaries[4]['namespace'] != boundaries[3]['namespace']
            assert 'This is a context handoff, not a new request.' in json.dumps(samples[-1])
            assert all(marker in json.dumps(samples[-1]) for marker in (
                'codex-shell-ok', 'codex-file-ok', 'codex-mcp-ok'))
            print(f'Native context recovery at cap {max_iterations}: '
                  f'{len(completed)} settled tools, {len(recovered)} public-context compaction, '
                  f'new invocation admitted at count {boundaries[-1]["model_calls"]}; '
                  f'final response: {result["final_response"]}.')
        agent.close()
        store.close()
        store = ContextStore(tmp_path / 'context.sqlite3', 'codex-transport')
        agent = create_agent()
        result = agent.run_conversation('Continue using the saved receipts.', conversation_history=[],
                                        system_message='requester-private-second-marker')
        expected_samples = 6 if outcome == 'context-recovery-limit' else 5
        assert result['completed'] and len(samples) == expected_samples and len(calls) == 1
        assert len(requests) == expected_samples + len(summaries)
        assert 'requester-private-first-marker' not in json.dumps(requests[-1])
        assert 'requester-private-second-marker' in json.dumps(requests[-1])
        assert 'codex-mcp-ok' in json.dumps(requests[-1])
        saved = ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
        assert 'requester-private-first-marker' not in saved
        assert 'requester-private-second-marker' not in saved
        assert 'native-private-summary-marker' not in saved
        assert 'native-private-summary-marker' not in json.dumps(requests[-1])
        assert not store.pending
        if outcome == 'context-recovery-limit':
            rows = (json.loads(row[0]) for row in store.db.execute('SELECT message FROM journal'))
            receipts = [message for message in rows if message.get('role') == 'tool']
            assert len(receipts) == 3
            assert len({receipt['tool_call_id'] for receipt in receipts}) == 3
            assert all(marker in json.dumps(receipts) for marker in (
                'codex-shell-ok', 'codex-file-ok', 'codex-mcp-ok'))
            assert sum(kind == 'start' for kind, _ in events) == 3
    finally:
        agent.close()
        store.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('yield_ms,outcome', [(1000, 'complete'), (None, 'complete'), (1000, 'interrupt')])
def test_native_code_mode_yield_polls_live_tool_without_checkpointing(tmp_path, monkeypatch, yield_ms, outcome):
    proof = native_yield_case(tmp_path, monkeypatch, yield_ms, outcome)
    assert not proof['faults'] and not proof['transport_errors']
    assert proof['completed'] is (outcome == 'complete'), proof
    assert proof['interrupted'] is (outcome == 'interrupt')
    assert not proof['failed'] and not proof['boundary_failed']
    assert proof['model_calls'] == proof['upstream_requests'] == (3 if outcome == 'complete' else 2)
    assert proof['lifecycle_steps'] == [0, 1]
    assert proof['tool_executions'] == proof['completed_receipts'] == 1
    assert proof['tool_events'] == ['start', 'complete']
    assert not proof['pending_tools'] and not proof['sdk_failure']


@pytest.mark.parametrize('outcome', ['settle-finite', 'settle-preview', 'settle-limit'])
def test_native_settlement_collects_late_command_receipts(tmp_path, monkeypatch, outcome):
    proof = native_yield_case(tmp_path, monkeypatch, 1000, outcome)
    limited = outcome == 'settle-limit'
    assert proof['native_plugins'] == [False]
    assert proof['completed'] is not limited, proof
    assert proof['pending_tools'] == int(limited)
    assert proof['completed_receipts'] == int(not limited)
    assert proof['upstream_requests'] == (2 if limited else 4)
    assert proof['tool_executions'] == 1
    assert 'premature-answer' not in proof['saved_prose']
    assert proof['final_response'] == ('Codex stopped (model call limit reached, HTTP 409, '
        '1 unresolved tool(s)). Saved tool receipts are preserved.' if limited else 'yield-test-complete')


def native_yield_case(tmp_path, monkeypatch, yield_ms, outcome='complete', *,
                      agent_class=CodexAgent, progress=lambda text: None):
    """Real Codex + MCP + relay, including the default ~30-second exec yield.

    The tool waits for the next model request to reach the upstream server.
    Requiring nested completion before admitting that request deadlocks.
    """
    from sandbox.broker_relay import BrokerRelay
    from sandbox.broker_transport import unseal
    capability = 'yield-fixture-capability'
    release = threading.Event()
    calls, requests, steps, events, faults, diagnostics = [], [], [], [], [], []
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    settlement = outcome.startswith('settle-')
    if settlement:
        (workspace / 'command.py').write_text(
            'import http.server, pathlib, signal, sys, time\n'
            'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n'
            'try:\n' + (
                ' server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)\n'
                ' pathlib.Path("preview-port").write_text(str(server.server_port))\n'
                ' print("preview-ready", flush=True)\n server.serve_forever()\n'
                if outcome == 'settle-preview' else
                ' while not pathlib.Path("release").exists(): time.sleep(0.05)\n') +
            'finally:\n print("slow-tool-complete", flush=True)\n')
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
    agent = None
    native_errors, native_plugins = [], []
    from openai_codex.async_client import AsyncCodexClient
    from sandbox.sdk_failure import codex_details
    class ObservedClient(AsyncCodexClient):
        async def initialize(self):
            from openai_codex.generated.v2_all import ExperimentalFeatureListResponse
            result = await super().initialize()
            features = await self.request('experimentalFeature/list', {},
                                         response_model=ExperimentalFeatureListResponse)
            native_plugins.extend(feature.enabled for feature in features.data if feature.name == 'plugins')
            progress('Native plugin marketplace enabled: ' + json.dumps(native_plugins))
            return result

        async def next_turn_notification(self, turn_id):
            event = await super().next_turn_notification(turn_id)
            if event.method == 'error':
                payload = event.payload.model_dump(mode='json', by_alias=True)
                detail = codex_details(payload.get('error'), will_retry=payload.get('willRetry'))
                native_errors.append(detail)
                progress('Native SDK error: ' + json.dumps(detail))
            return event
    monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', ObservedClient)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass  # The baseline SDK exits with the fixture tool unresolved.

        def do_GET(self):
            if self.path == '/context/window':
                return self.reply({'input_budget': 200000})
            assert self.path == '/tools'
            self.reply([{'name': 'slow_echo', 'description': 'Wait for a local fixture signal.',
                'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}])

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(unseal(capability, self.path, raw))
            if self.path == '/context/maintenance':
                if agent.journal is not None:
                    assert not agent.journal.pending
                return self.reply({})
            if self.path == '/tools/call':
                calls.append(body)
                progress('Tool started; it waits for an admitted model poll')
                if not release.wait(timeout=50):
                    faults.append('Model polling was blocked while the tool was running')
                return self.reply({'text': 'slow-tool-complete'})
            assert self.path == '/v1/responses'
            requests.append(body)
            sequence = len(requests)
            progress(f'Model request {sequence} admitted; pending tools: {len(agent.journal.pending)}')
            if sequence == 1:
                code = ('text(await tools.exec_command(' + json.dumps({
                    'cmd': shlex.quote(sys.executable) + ' command.py', 'tty': True,
                    'login': False, 'yield_time_ms': 1000}) + '));' if settlement else
                    'text(await tools.mcp__moyai__slow_echo({}));')
                if yield_ms is not None and not settlement:
                    code = '// @exec: ' + json.dumps({'yield_time_ms': yield_ms}) + '\n' + code
                output = {'type': 'custom_tool_call', 'id': 'exec_1', 'call_id': 'outer_1',
                          'name': 'exec', 'namespace': 'functions', 'input': code}
            elif settlement and sequence == 2:
                assert agent.journal.pending and steps == [0]
                if outcome == 'settle-preview':
                    from urllib.request import urlopen
                    port = (workspace / 'preview-port').read_text()
                    with urlopen(f'http://127.0.0.1:{port}/command.py', timeout=2) as response:
                        assert response.status == 200
                    progress('Preview server answered HTTP 200; model prematurely finishes')
                output = {'type': 'message', 'id': 'premature', 'role': 'assistant',
                    'phase': 'final_answer', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'premature-answer'}]}
            elif settlement and sequence == 3:
                assert agent.journal.pending and agent.model_calls == 3
                sessions = re.findall(r'session_id\\?"\s*:\s*(\d+)', json.dumps(body))
                assert sessions, 'Native command must return a running session'
                (workspace / 'release').touch()
                progress('Settlement turn stops the preview or waits for finite work')
                output = {'type': 'custom_tool_call', 'id': 'settle', 'call_id': 'settle',
                    'name': 'exec', 'namespace': 'functions',
                    'input': 'text(await tools.write_stdin(' + json.dumps({
                        'session_id': int(sessions[-1]), 'yield_time_ms': 1000,
                        'chars': '\u0003' if outcome == 'settle-preview' else ''}) + '));'}
            elif sequence == 2:
                assert len(calls) == 1 and agent.journal.pending
                assert steps == [0], 'Lifecycle must not run while the nested tool is pending'
                assert agent.model_calls == 2, 'Polling must count toward the model-call cap'
                cells = re.findall(r'Script running with cell ID ([0-9]+)', json.dumps(body))
                assert cells, 'The native runtime must actually yield'
                output = {'type': 'function_call', 'id': 'wait_2', 'call_id': 'outer_2',
                          'name': 'wait', 'namespace': 'functions',
                          'arguments': json.dumps({'cell_id': cells[-1], 'yield_time_ms': 1000})}
                release.set()
            else:
                if not settlement:
                    assert not agent.journal.pending and agent.journal.completed_tools == 1
                assert 'slow-tool-complete' in json.dumps(body)
                output = {'type': 'message', 'id': 'answer', 'role': 'assistant', 'phase': 'final_answer',
                          'status': 'completed', 'content': [{'type': 'output_text', 'text': 'yield-test-complete'}]}
            send_response(self, output, sequence)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', capability,
                        report_error=diagnostics.append).start()
    store = ContextStore(tmp_path / 'context.sqlite3', 'yield-test')
    store.initialize([])

    def step():
        assert not agent.journal.pending
        steps.append(agent.journal.completed_tools)
        progress(f'Settled lifecycle check; completed tools: {agent.journal.completed_tools}')
        if outcome == 'interrupt' and agent.journal.completed_tools:
            agent.interrupt()

    agent = agent_class(spec={'model': 'openai/gpt-6-astra', 'timeout': 60,
        'max_iterations': 2 if outcome == 'settle-limit' else 6},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
            complete=lambda *args: events.append(('complete', args)), commentary=lambda text: None),
        step=step, cwd=str(workspace), definition=None, context_store=store)
    try:
        result = agent.run_conversation('Run the local slow tool once.', conversation_history=[],
                                        system_message='Local yield regression fixture.')
        assert calls == ([] if settlement else [{'name': 'slow_echo', 'arguments': {}}])
        receipts = [m for m in result['messages'] if m['role'] == 'tool']
        assert all('slow-tool-complete' in receipt['content'] for receipt in receipts)
        proof = {key: result[key] for key in ('completed', 'interrupted', 'failed', 'final_response')}
        proof.update(boundary_failed=agent.boundary_failed, model_calls=agent.model_calls,
            upstream_requests=len(requests), lifecycle_steps=steps,
            tool_executions=sum(kind == 'start' for kind, _ in events),
            completed_receipts=len(receipts), pending_tools=len(store.pending),
            tool_events=[kind for kind, _ in events], faults=faults, transport_errors=diagnostics,
            sdk_failure=result.get('sdk_failure'), native_errors=native_errors, native_plugins=native_plugins,
            saved_prose='\n'.join(m.get('content') or '' for m in result['messages'] if m['role'] == 'assistant'))
        progress('Result: ' + json.dumps({key: proof[key] for key in (
            'completed', 'boundary_failed', 'upstream_requests', 'tool_executions', 'completed_receipts', 'pending_tools')}))
        return proof
    finally:
        release.set()
        (workspace / 'release').touch()
        agent.close()
        relay.close()
        store.close()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('during', ['generation', 'tool', 'completion'])
def test_send_now_reaches_live_native_session_before_work_finishes(tmp_path, monkeypatch, during):
    proof = native_steering_case(tmp_path, monkeypatch, during)
    assert proof['completed'] and proof['root_turn_unchanged'], proof
    assert proof['accepted_before_release'] == 2
    assert proof['corrections_reached_model'] == 2
    assert proof['tool_executions'] == proof['completed_receipts'] == int(during == 'tool')
    if during == 'completion':
        assert proof['steer_results'] and set(proof['steer_results']) == {'no active turn to steer'}


def native_steering_case(tmp_path, monkeypatch, during, *, progress=lambda text: None):
    """Pinned native SDK + actual control HTTP/SQLite, with held local inference/tool."""
    from concurrent.futures import ThreadPoolExecutor
    import time
    from app.db import Store
    from app.message_queue import MessageQueue
    from sandbox.broker_relay import BrokerRelay
    from sandbox.broker_transport import unseal
    from sandbox.continuation import ActiveTurnSteering, AgentSteer
    from sandbox.harness_registry import resolve
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.errors import InvalidRequestError
    import asyncio

    store = Store(tmp_path / 'app')
    run = store.create_run('Finish the local task', '', 'modal', [], chat_enabled=True,
        user_id='fixture-user', model='openai/gpt-6-astra', harness='codex')
    run_id = run['id']
    root = store.claim_message(run_id)['id']
    store.update_run(run_id, status='running')
    queue = MessageQueue(store)
    entered, release = threading.Event(), threading.Event()
    requests, controls, calls, latencies = [], [], [], []
    steer_results = []
    capability = 'local-steering-fixture'
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)

    if during == 'completion':
        class CompletionClient(AsyncCodexClient):
            async def next_turn_notification(self, turn_id):
                event = await super().next_turn_notification(turn_id)
                if event.method == 'turn/completed' and not entered.is_set():
                    # Hold an actual native completion in the adapter's pending
                    # notification task while new input targets that same turn.
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 15)
                return event

            async def turn_steer(self, *args, **kwargs):
                try:
                    result = await super().turn_steer(*args, **kwargs)
                except InvalidRequestError as exc:
                    steer_results.append(exc.message)
                    raise
                steer_results.append('accepted')
                return result

        monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', CompletionClient)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.reply({'input_budget': 200000} if self.path == '/context/window' else [
                {'name': 'slow_echo', 'description': 'Wait for a local fixture signal.',
                 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}])

        def do_POST(self):
            body = json.loads(unseal(capability, self.path,
                self.rfile.read(int(self.headers['Content-Length']))))
            if self.path == '/control':
                controls.append(body)
                control = (queue.live_control(run_id, root, body.get('applied', []))
                    if body.get('version') == 2 else {'steer_message_id': queue.accept_steer(run_id, root)})
                return self.reply({**control, 'receipt_only_supported': True})
            if self.path == '/context/maintenance':
                return self.reply({})
            if self.path == '/tools/call':
                calls.append(body)
                progress('Real MCP tool is running; completed effects must not be replayed.')
                entered.set()
                assert release.wait(15)
                return self.reply({'text': 'tool-receipt-once'})
            assert self.path == '/v1/responses'
            requests.append(body)
            sequence = len(requests)
            if sequence == 1 and during == 'tool':
                output = {'type': 'custom_tool_call', 'id': 'exec_fixture', 'call_id': 'exec_fixture',
                    'name': 'exec', 'namespace': 'functions', 'input': 'text(await tools.mcp__moyai__slow_echo({}));'}
            else:
                if sequence == 1 and during == 'generation':
                    progress('Native Codex inference is running; the response is held open.')
                    entered.set()
                    assert release.wait(15)
                output = {'type': 'message', 'id': 'answer-' + str(sequence), 'role': 'assistant',
                    'phase': 'final_answer', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Local task finished with saved corrections.'}]}
            send_response(self, output, sequence)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', capability).start()
    definition = resolve('codex')
    steering = ActiveTurnSteering(relay) if definition.live_steering else AgentSteer(relay)
    relay.steering = steering
    agent = CodexAgent(spec={'model': 'openai/gpt-6-astra', 'max_iterations': 6, 'timeout': 30},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}}}},
        activity=SimpleNamespace(start=lambda *a: None, complete=lambda *a: None, commentary=lambda *a: None),
        step=lambda: steering.step(agent), cwd=str(tmp_path), definition=definition)
    steering.listen(agent)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(agent.run_conversation, 'Finish the local task.',
            conversation_history=[], system_message='Local steering verification.')
        assert entered.wait(12), 'Native runtime did not reach the local fixture'
        previous = len(controls)
        for index in range(2):
            marker = f'correction-marker-{index}'
            progress(f'Send now: {marker}')
            start = time.monotonic()
            message, _ = store.enqueue_message(run_id, marker, marker, user_id='fixture-user',
                model='openai/gpt-6-astra', send_now=True)
            while time.monotonic() - start < 4:
                row = next(m for m in store.messages(run_id) if m['id'] == message['id'])
                if row['status'] == 'injected':
                    break
                time.sleep(.01)
            assert row['status'] == 'injected', 'Send now stayed queued while native work was active'
            elapsed = round((time.monotonic() - start) * 1000)
            latencies.append(elapsed)
            progress(f'Runtime accepted and saved the correction in {elapsed} ms; work is still active.')
        assert not future.done()
        release.set()
        result = future.result(timeout=20)
        proof = {'during': during, 'completed': result['completed'], 'accepted_before_release': len(latencies),
            'acceptance_ms': latencies, 'control_polls_during_work': len(controls) - previous,
            'root_turn_unchanged': store.run(run_id)['active_message_id'] == root,
            'corrections_reached_model': sum(any(f'correction-marker-{i}' in json.dumps(body)
                for body in requests) for i in range(2)), 'tool_executions': len(calls),
            'completed_receipts': sum(m['role'] == 'tool' for m in result['messages']),
            'steer_results': steer_results}
        progress('Verified: ' + json.dumps(proof))
        return proof
    finally:
        release.set()
        executor.shutdown(wait=True)
        steering.close()
        agent.close()
        relay.close()
        server.shutdown()
        server.server_close()
