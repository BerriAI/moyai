"""Real bundled SDK process and real MCP transport; inference is a local fixture.

No provider calls or generated shell commands. Tools read and echo synthetic data.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import shutil
import threading
from types import SimpleNamespace

import pytest

from sandbox.claude_harness import ClaudeAgent
from sandbox.context_store import ContextStore


@pytest.fixture
def broker_workspace(tmp_path, monkeypatch):
    # The standalone Linux guest conformance test only needs local inference
    # and MCP. Load the web app only for the native broker integration cases.
    from test_workspace import workspace
    yield from workspace.__wrapped__(tmp_path, monkeypatch)


def test_claude_background_compaction_keeps_running_sdk_and_new_tail(tmp_path, monkeypatch, broker_workspace):
    from test_codex_sdk_transport import native_background_case
    native_background_case(tmp_path, monkeypatch, broker_workspace, 'claude-agent-sdk', progress=print)


def broker_recovery_case(tmp_path, monkeypatch, progress=lambda message: None):
    """Real SDK/MCP recovery proof, also callable by the local recording demo."""
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage
    from sandbox.activity import ActivityReporter
    from sandbox.broker_relay import BrokerRelay
    from sandbox.broker_transport import unseal
    from sandbox.transport_recovery import recovery_marker, validate_recovery

    capability = 'recovery-fixture-capability'
    request_id = 'render-recovery-request-502'
    receipt = 'publication-receipt-001'
    effects = tmp_path / 'publications.txt'
    calls, requests, failures, native_actions, diagnostics, native_exits = [], [], [], [], [], []
    restored = False
    receive_messages = ClaudeSDKClient.receive_messages

    async def receive_until_native_exit(client):
        async for message in receive_messages(client):
            if not restored and isinstance(message, ResultMessage) and message.is_error:
                # A vanished native process cannot perform live continuation.
                # Kill the real child before the adapter sees its error result;
                # the already-saved publication receipt authorizes cold recovery.
                process = client._transport._process
                process.kill()
                native_exits.append(await process.wait())
                progress('Native process exited before delivering its error result.')
                return
            yield message

    monkeypatch.setattr(ClaudeSDKClient, 'receive_messages', receive_until_native_exit)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value, status=200, content_type='application/json', headers=None):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/context/window':
                return self.reply({'input_budget': 200_000})
            assert self.path == '/tools'
            self.reply([{'name': 'publish_fixture', 'description': 'Publish one synthetic fixture record.',
                         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}])

        def do_POST(self):
            assert self.headers['Authorization'] == 'Bearer ' + capability
            body = self.rfile.read(int(self.headers['Content-Length']))
            data = json.loads(unseal(capability, self.path, body))
            if self.path == '/context/maintenance':
                return self.reply({})
            if self.path == '/context/native':
                native_actions.append(data['action'])
                return self.reply({'lease': data['lease'], 'state': None, 'reason': 'fresh'})
            if self.path == '/tools/call':
                assert data == {'name': 'publish_fixture', 'arguments': {}}
                calls.append(data)
                with effects.open('a') as handle:
                    handle.write(receipt + '\n')
                progress('Tool completed: one publication; receipt saved.')
                return self.reply({'receipt': receipt})
            assert self.path == '/v1/messages'
            requests.append(data)
            if calls and not restored:
                failures.append(True)
                progress('Injected model HTTP 502; request ID: ' + request_id)
                return self.reply(b'<html>Cloud edge unavailable</html>', status=502,
                    content_type='text/html', headers={'X-Request-ID': request_id})
            if restored:
                assert receipt in json.dumps(data['messages'])
                assert 'Do not publish twice' in json.dumps(data['messages'])
                block = {'type': 'text', 'text': 'Recovered from the saved publication receipt.'}
            elif any(tool['name'] == 'mcp__moyai__publish_fixture' for tool in data['tools']):
                block = {'type': 'tool_use', 'id': 'publish_once', 'name': 'mcp__moyai__publish_fixture', 'input': {}}
            else:
                block = {'type': 'tool_use', 'id': 'find_publication', 'name': 'ToolSearch',
                         'input': {'query': 'select:mcp__moyai__publish_fixture', 'max_results': 1}}
            text = block['type'] == 'text'
            message = {'id': 'msg_' + str(len(requests)), 'type': 'message', 'role': 'assistant',
                       'model': 'claude-sonnet-4-5', 'content': [block],
                       'stop_reason': 'end_turn' if text else 'tool_use', 'stop_sequence': None,
                       'usage': {'input_tokens': 50, 'output_tokens': 10}}
            if not data.get('stream'):
                return self.reply(message)
            frames = [dict(type='message_start', message={**message, 'content': [], 'stop_reason': None}),
                      dict(type='content_block_start', index=0,
                           content_block={'type': 'text', 'text': ''} if text else {**block, 'input': {}}),
                      dict(type='content_block_delta', index=0,
                           delta={'type': 'text_delta', 'text': block['text']} if text else
                                 {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}),
                      dict(type='content_block_stop', index=0),
                      dict(type='message_delta', delta={'stop_reason': message['stop_reason'], 'stop_sequence': None},
                           usage={'output_tokens': 10}),
                      dict(type='message_stop')]
            self.reply(''.join('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n'
                              for frame in frames).encode(), content_type='text/event-stream')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-config'))
    workspace, session = tmp_path / 'workspace', tmp_path / 'session'
    workspace.mkdir()
    session.mkdir()
    store = ContextStore(session / 'context.sqlite3', 'transport-recovery')
    store.initialize([])
    relay, agent = None, None

    def start_agent(marker=None):
        relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', capability,
                            report_error=diagnostics.append).start()
        agent = ClaudeAgent(spec={'model': 'anthropic/claude-sonnet-4-5', 'timeout': 30, 'max_iterations': 4,
                                 'history_reference_dir': str(store.path.parent),
                                 **({'transport_recovery': marker, 'continuation': True} if marker else {})},
            relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
                'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}}}},
            activity=ActivityReporter(lambda *args: None),
            step=lambda: None, cwd=str(workspace), definition=None, context_store=store)
        return relay, agent

    try:
        relay, agent = start_agent()
        result = agent.run_conversation('Publish the fixture. Do not publish twice.', conversation_history=[],
                                        system_message='Transport recovery fixture.')
        assert result['failed'] and not result['completed']
        assert len(native_exits) == 1 and native_exits[0] != 0
        assert agent.transport_attempt == 0, 'Cold recovery starts only after the native owner has exited.'
        assert effects.read_text().splitlines() == [receipt]
        assert not agent.journal.pending and not store.pending
        assert failures == [True], 'The SDK or relay must not resubmit the failed model request.'
        marker = recovery_marker(agent, result)
        assert marker and marker['checkpoint'] == store.checkpoint()
        assert marker['failure']['request_ids']['x-request-id'] == request_id
        assert marker['failure']['http_status'] == 502
        assert marker['failure']['response_started'] is False
        assert diagnostics == [marker['failure']]
        assert capability not in json.dumps(diagnostics)
        failed_request_count = len(requests)
        agent.close()
        agent = None
        relay.close()
        relay = None
        store.close()
        cold = tmp_path / 'cold-session'
        cold.mkdir()
        shutil.copy2(session / 'context.sqlite3', cold / 'context.sqlite3')
        store = ContextStore(cold / 'context.sqlite3', 'transport-recovery')
        validate_recovery(store, marker)
        native_action_count = len(native_actions)
        progress('Cold checkpoint restored and verified. Starting a fresh SDK from saved receipts.')
        restored = True
        relay, agent = start_agent(marker)
        result = agent.run_conversation('Continue the unfinished task from its saved receipts. Do not publish twice.',
                                        conversation_history=[], system_message='Transport recovery fixture.')
        assert result['completed'], result['final_response']
        assert len(requests) == failed_request_count + 1
        assert calls == [{'name': 'publish_fixture', 'arguments': {}}]
        assert effects.read_text().splitlines() == [receipt]
        assert native_actions[native_action_count] == 'restart'
        assert not agent.journal.pending and not store.pending
        progress('Recovered successfully. Publication count: 1; no completed action repeated.')
        return {'request_id': request_id, 'http_status': marker['failure']['http_status'],
                'tool_executions': len(calls), 'failed_requests': len(failures), 'answer': result['final_response']}
    finally:
        if agent is not None:
            agent.close()
        if relay is not None:
            relay.close()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_sdk_recovers_broker_failure_from_cold_tool_receipts(tmp_path, monkeypatch):
    broker_recovery_case(tmp_path, monkeypatch)


@pytest.mark.parametrize('resumed', [False, True, 'durable', 'pressure', 'pending', 'native', 'native-pressure', 'native-corrupt',
                                   'steer-generation', 'steer-tool'],
                         ids=['new-session', 'large-checkpoint', 'durable-checkpoint', 'context-pressure', 'interrupted-tool', 'native-resume', 'native-pressure', 'native-corrupt',
                              'steer-generation', 'steer-tool'])
@pytest.mark.parametrize('model', ['anthropic/claude-sonnet-4-5', 'openai/gpt-6-astra', 'fireworks_ai/glm-5p3'])
def test_real_sdk_executes_mcp_and_preserves_receipt(tmp_path, monkeypatch, resumed, model, request):
    calls, requests, events = [], [], []
    rejected = []
    native_case = resumed in {'native', 'native-pressure', 'native-corrupt'}
    steering_case = resumed in {'steer-generation', 'steer-tool'}
    steering_sent = threading.Event()
    accepted_inputs = []

    def steer():
        accepted_inputs.append(agent.accept_input({'id': 17, 'content': 'Retain the original task. Use steering-marker.'}))
        assert accepted_inputs == [True]
        assert steering_sent.wait(3), 'The active SDK must accept input before the blocked work completes'

    pressure_case = resumed in {'pressure', 'native-pressure'}
    durable_case = resumed in {'durable', 'pressure', 'pending'} or native_case
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            assert self.path == '/tools'
            self.reply([{'name': 'echo', 'description': 'Echo synthetic test data',
                         'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
                        *[{'name': f'unused_{i}', 'description': f'Unrelated catalog operation {i}.',
                           'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}
                          for i in range(80)]])
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/tools/call':
                if resumed == 'steer-tool':
                    steer()
                calls.append(data)
                return self.reply({'text': data['arguments']['text']})
            assert self.path.startswith('/v1/messages')
            requests.append(data)
            if resumed == 'steer-generation' and len(requests) == 1:
                steer()
            if pressure_case and calls and not rejected:
                rejected.append(True)
                relay.context_required = {'input_tokens': 50000, 'input_budget': 20000}
                return self.reply({'type': 'error', 'error': {'type': 'invalid_request_error',
                    'message': 'Context handoff requested', 'code': 'context_length_exceeded'}}, status=400)
            # A fresh SDK session cannot compact an oversized first message.
            if len(json.dumps(data['messages']).encode()) > 100_000:
                return self.reply({'type': 'error', 'error': {'type': 'invalid_request_error',
                    'message': 'Prompt is too long'}}, status=400)
            done = bool(calls)
            receipt = next((block for message in data['messages'] for block in message.get('content', [])
                            if isinstance(block, dict) and block.get('tool_use_id') == 'history_read'), None)
            if durable_case:
                text = json.dumps(data['messages'])
                assert 'Keep Escape support' in text and 'Do not deploy' in text
                if resumed == 'pending':
                    assert 'UNRESOLVED TOOL OUTCOMES' in text
                if calls:
                    assert 'sdk-transport-ok' in text
            inspect_file = resumed is True or (resumed == 'pending' and not done)
            if inspect_file and receipt is None:
                block = {'type': 'tool_use', 'id': 'history_read', 'name': 'Read', 'input': {
                    'file_path': str(workspace / 'note.txt' if resumed == 'pending' else session / '.moyai-history.jsonl'),
                    'offset': 1, 'limit': 1}}
            else:
                if inspect_file:
                    assert not receipt.get('is_error'), receipt
                    expected_read = 'already edited' if resumed == 'pending' else 'Continue the searchable dropdown task'
                    assert expected_read in json.dumps(receipt)
                block = ({'type': 'text', 'text': 'sdk-transport-ok'} if done else
                         {'type': 'tool_use', 'id': 'tool_echo', 'name': 'mcp__moyai__echo', 'input': {'text': 'sdk-transport-ok'}})
                if not done and not any(tool['name'] == 'mcp__moyai__echo' for tool in data['tools']):
                    block = {'type': 'tool_use', 'id': 'search_echo', 'name': 'ToolSearch',
                             'input': {'query': 'select:mcp__moyai__echo', 'max_results': 1}}
            message = {'id': 'msg_' + str(len(requests)), 'type': 'message', 'role': 'assistant',
                       'model': 'claude-sonnet-4-5', 'content': [block], 'stop_reason': 'end_turn' if done else 'tool_use',
                       'stop_sequence': None, 'usage': {'input_tokens': 50, 'output_tokens': 10}}
            if not data.get('stream'): return self.reply(message)
            start = {**message, 'content': [], 'stop_reason': None}
            delta = ({'type': 'text_delta', 'text': block['text']} if done else
                     {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])})
            empty = {'type': 'text', 'text': ''} if done else {**block, 'input': {}}
            frames = [dict(type='message_start', message=start),
                      dict(type='content_block_start', index=0, content_block=empty),
                      dict(type='content_block_delta', index=0, delta=delta),
                      dict(type='content_block_stop', index=0),
                      dict(type='message_delta', delta={'stop_reason': message['stop_reason'], 'stop_sequence': None}, usage={'output_tokens': 10}),
                      dict(type='message_stop')]
            wire = ''.join('event: ' + f['type'] + '\ndata: ' + json.dumps(f) + '\n\n' for f in frames).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'fixture-capability')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-config'))
    relay = SimpleNamespace(url=url)
    native_sessions, native_prompts = [], []
    if native_case:
        from app.security import digest
        from sandbox.broker_transport import CONTENT_TYPE, seal
        from claude_agent_sdk import ClaudeSDKClient, ResultMessage
        app, client = request.getfixturevalue('broker_workspace')
        app.state.settings.agent_model = model
        db = app.state.store
        run = db.create_run('Call the echo tool.', '', 'modal', [], chat_enabled=True,
            harness='claude-agent-sdk', model=model, user_id='google:native-fixture')
        current_message = db.claim_message(run['id'])
        db.update_run(run['id'], status='running', token_hash=digest('fixture-capability'))
        def native(body):
            route = '/context/native'
            response = client.post('/broker/' + run['id'] + route,
                content=seal('fixture-capability', route, json.dumps(body).encode()),
                headers={'Authorization': 'Bearer fixture-capability', 'Content-Type': CONTENT_TYPE})
            assert response.status_code == 200, response.text
            return response.json()
        relay.native = native
        original_query, original_receive = ClaudeSDKClient.query, ClaudeSDKClient.receive_messages
        async def query(client, prompt, *args, **kwargs):
            native_prompts.append(prompt)
            return await original_query(client, prompt, *args, **kwargs)
        async def receive(client, *args, **kwargs):
            async for message in original_receive(client, *args, **kwargs):
                if isinstance(message, ResultMessage):
                    native_sessions.append(message.session_id)
                yield message
        monkeypatch.setattr(ClaudeSDKClient, 'query', query)
        monkeypatch.setattr(ClaudeSDKClient, 'receive_messages', receive)
    workspace, session = tmp_path / 'workspace', tmp_path / 'session'
    workspace.mkdir()
    session.mkdir()
    def create_agent(context_store=None):
        return ClaudeAgent(spec={'model': model, 'timeout': int(os.environ.get('MOYAI_SDK_TEST_TIMEOUT', '30')), 'max_iterations': 4,
                             'history_reference_dir': str(session)},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': url, 'WORKSPACE_RUN_TOKEN': 'fixture-capability'}}}},
        activity=SimpleNamespace(start=lambda *a: events.append(('start', a)),
            complete=lambda *a: events.append(('complete', a)), commentary=lambda text: None),
        step=lambda: None, cwd=str(workspace), definition=None, context_store=context_store)
    agent = create_agent()
    if steering_case:
        from claude_agent_sdk import ClaudeSDKClient
        original_query = ClaudeSDKClient.query

        async def query(client, prompt, *args, **kwargs):
            await original_query(client, prompt, *args, **kwargs)
            if not isinstance(prompt, str):
                steering_sent.set()

        monkeypatch.setattr(ClaudeSDKClient, 'query', query)
    history = ([{'role': 'user', 'content': 'Continue the searchable dropdown task; do not repeat completed writes.'},
                {'role': 'assistant', 'tool_calls': [{'id': 'read1', 'type': 'function', 'function': {
                    'name': 'Read', 'arguments': '{"file_path":"build.log"}'}}]},
                {'role': 'tool', 'tool_call_id': 'read1', 'content': '\n'.join(
                    f'Build output {i}: completed operation with verbose diagnostic details.' for i in range(5000))}]
               if resumed else [])
    store = None
    if durable_case:
        store = ContextStore(session / 'context.sqlite3', 'transport-run')
        if resumed == 'pending':
            (workspace / 'note.txt').write_text('already edited\n')
            history = [{'role': 'assistant', 'tool_calls': [{'id': 'history_read', 'function': {
                'name': 'Edit', 'arguments': json.dumps({'file_path': str(workspace / 'note.txt')})}}]}]
        store.initialize([{'role': 'user', 'content': 'Keep Escape support. Do not deploy.'}, *history,
            *[{'role': 'assistant', 'content': 'Older completed step ' + str(i)} for i in range(80)]])
        def compact(previous, entries, **kwargs):
            # Deterministic tool-free inference fixture, separately exercised
            # through the authenticated endpoint in test_context_gateway.py.
            source = previous + json.dumps(entries)
            return '\n'.join(fact for fact in ['Keep Escape support', 'Do not deploy', 'sdk-transport-ok'] if fact in source)
        relay.compact = compact
        agent = create_agent(store)
    try:
        result = agent.run_conversation('Call the echo tool.', conversation_history=history, system_message='Transport fixture.')
        assert result['completed'], result['final_response']
        assert result['final_response'] == 'sdk-transport-ok'
        if steering_case:
            assert accepted_inputs == [True] and steering_sent.is_set()
            assert 'steering-marker' in json.dumps(requests[-1]['messages'])
            assert sum('steering-marker' in str(message.get('content')) for message in result['messages']) == 1
            assert not agent.accept_input({'id': 18, 'content': 'This input belongs to the next invocation.'})
        assert calls == [{'name': 'echo', 'arguments': {'text': 'sdk-transport-ok'}}]
        expected = (['Read'] if resumed is True or resumed == 'pending' else []) + ['ToolSearch', 'mcp__moyai__echo']
        assert [args[1] for kind, args in events if kind == 'start'] == expected
        assert [kind for kind, _ in events] == ['start', 'complete'] * len(expected)
        first_tools = {tool['name'] for tool in requests[0]['tools']}
        assert {'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'ToolSearch'} <= first_tools
        assert not any(name.startswith('mcp__') for name in first_tools)
        assert not any('unused_' in tool['name'] for body in requests for tool in body['tools'])
        discovered = next(body for body in reversed(requests)
                          if any(t['name'] == 'mcp__moyai__echo' for t in body['tools']))
        last_tools = {tool['name']: tool for tool in discovered['tools']}
        assert last_tools['mcp__moyai__echo']['input_schema']['properties'] == {'text': {'type': 'string'}}
        assert last_tools['mcp__moyai__echo']['defer_loading'] is True
        assert 'tool_reference' in json.dumps(discovered['messages'])
        if pressure_case:
            assert not any(t['name'] == 'mcp__moyai__echo' for t in requests[-1]['tools'])
        assert not agent.journal.pending
        assert any(m.get('role') == 'tool' and 'sdk-transport-ok' in m['content'] for m in result['messages'])
        assert any('cache_control' in json.dumps(body) for body in requests)
        if durable_case:
            if resumed == 'pending':
                assert store.pending == {'history_read'}
                assert (workspace / 'note.txt').read_text() == 'already edited\n'
            store.compact(relay.compact)
            cursor = store.state()['cursor']
            assert cursor > 0
            agent.close()
            if native_case:
                stored = db.rows('SELECT encrypted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['encrypted']
                assert stored, 'The successful recovery must save a new native checkpoint'
                assert 'sdk-transport-ok' not in stored
                completed_session = native_sessions[-1]
                if resumed == 'native-corrupt':
                    envelope = json.loads(app.state.security.decrypt(stored))
                    envelope['state']['records'] = 'invalid transcript'
                    db.execute('UPDATE native_sessions SET encrypted=? WHERE run_id=?',
                               (app.state.security.encrypt(json.dumps(envelope)), run['id']))
                db.finish_message(run['id'], current_message['id'], result['final_response'])
                db.update_run(run['id'], status='idle')
                db.enqueue_message(run['id'], 'Continue using the saved receipts.', 'native-follow-up',
                                   user_id='google:native-fixture', model=model)
                current_message = db.claim_message(run['id'])
                db.update_run(run['id'], status='running')
            store.close()
            cold = tmp_path / 'cold-session'
            cold.mkdir()
            shutil.copy2(session / 'context.sqlite3', cold / 'context.sqlite3')
            store = ContextStore(cold / 'context.sqlite3', 'transport-run')
            assert store.state()['cursor'] == cursor
            agent = create_agent(store)
            result = agent.run_conversation('Continue using the saved receipts.', conversation_history=[], system_message='Transport fixture.')
            assert result['completed'], result['final_response']
            assert calls == [{'name': 'echo', 'arguments': {'text': 'sdk-transport-ok'}}]
            if native_case and resumed != 'native-corrupt':
                assert native_sessions[-1] == completed_session
                assert native_prompts[-1] == 'Continue using the saved receipts.'
                assert agent.native.reason == 'resumed'
                assert len(native_prompts) == (3 if pressure_case else 2)
            if resumed == 'native-corrupt':
                assert len(native_prompts) == 2  # Corrupt state was rejected before starting another SDK.
                assert native_prompts[-1] != 'Continue using the saved receipts.'
                assert native_sessions[-1] != completed_session
                completed_session = native_sessions[-1]
                agent.close()
                rebuilt = db.rows('SELECT encrypted FROM native_sessions WHERE run_id=?', (run['id'],))[0]['encrypted']
                assert rebuilt, 'The successful fresh fallback must replace the corrupt native checkpoint'
                db.finish_message(run['id'], current_message['id'], result['final_response'])
                db.update_run(run['id'], status='idle')
                db.enqueue_message(run['id'], 'One more question.', 'native-third-turn',
                                   user_id='google:native-fixture', model=model)
                db.claim_message(run['id'])
                db.update_run(run['id'], status='running')
                store.close()
                third = tmp_path / 'third-session'
                third.mkdir()
                shutil.copy2(cold / 'context.sqlite3', third / 'context.sqlite3')
                store = ContextStore(third / 'context.sqlite3', 'transport-run')
                agent = create_agent(store)
                result = agent.run_conversation('One more question.', conversation_history=[], system_message='Transport fixture.')
                assert result['completed'], result['final_response']
                assert native_sessions[-1] == completed_session and agent.native.reason == 'resumed'
                assert native_prompts[-1] == 'One more question.' and len(native_prompts) == 3
                assert calls == [{'name': 'echo', 'arguments': {'text': 'sdk-transport-ok'}}]
            assert [args[1] for kind, args in events if kind == 'start'] == expected
            if resumed == 'pending':
                assert store.pending == {'history_read'}
            assert not (session / '.moyai-history.jsonl').exists()
            if pressure_case:
                assert rejected == [True]
        else:
            assert result['messages'][:len(history)] == history
        if resumed is True:
            saved = [json.loads(line) for line in (session / '.moyai-history.jsonl').read_text().splitlines()]
            assert saved == history
            assert not (workspace / '.moyai-history.jsonl').exists()
    finally:
        agent.close()
        if store is not None:
            store.close()
        server.shutdown()
        server.server_close()


def live_recovery_case(tmp_path, monkeypatch, *, outage_seconds=3, progress=print, on_outage=lambda: None, on_status=None, delay_continuation=False, late_blocked_result=False):
    from dataclasses import replace
    import asyncio
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage
    import shlex
    import time
    from sandbox.activity import ActivityReporter
    from sandbox.broker_relay import BrokerRelay, InputPending
    from sandbox.broker_transport import unseal
    from test_claude_native_compaction import send_message

    capability = 'claude-recovery-test'
    correction = 'Preserve the original task. Inspect the recovered command before any new action.'
    requests, diagnostics, events, pids = [], [], [], []
    state = {'failed_at': None, 'probes': 0, 'outage_elapsed': 0, 'blocked': 0, 'late_result': False}
    effects, ready, completed = [tmp_path / name for name in ('executions.txt', 'ready', 'completed')]
    command = ('import os,time; from pathlib import Path; '
               f'Path({str(effects)!r}).open("a").write(str(os.getpid())+"\\n"); '
               f'print("original-command-running",flush=True); '
               f'\nwhile not Path({str(ready)!r}).exists(): time.sleep(.05)\n'
               f'Path({str(completed)!r}).write_text("original-command-completed"); print("original-command-completed")')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/context/window':
                return self.reply({'input_budget': 200000})
            assert self.path == '/v1/models', self.path
            state['probes'] += 1
            if state['probes'] == 1:
                assert agent.accept_input({'id': 42, 'content': correction})
                if late_blocked_result:
                    ready.touch()  # Finish while the relay's failed-model gate is still closed.
            if time.monotonic() - state['failed_at'] < outage_seconds:
                return self.reply({'error': 'broker restarting'}, 503)
            state['outage_elapsed'] = time.monotonic() - state['failed_at']
            ready.touch()
            progress('Broker restored; original command is allowed to finish.')
            return self.reply({'object': 'list', 'data': []})
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            data = json.loads(unseal(capability, self.path, body))
            if self.path == '/context/maintenance':
                return self.reply({})
            if self.path == '/context/native':
                return self.reply({'lease': data['lease'], 'state': None, 'reason': 'fresh'})
            assert self.path == '/v1/messages', self.path
            requests.append(data)
            if len(requests) == 1:
                progress('Model request 1 admitted; starting the original command.')
                block = {'type': 'tool_use', 'id': 'original_command', 'name': 'Bash', 'input': {
                    'command': shlex.join([sys.executable, '-u', '-c', command]), 'timeout': 1000,
                    'description': 'Run the original finite command'}}
            elif state['failed_at'] is None:
                assert effects.exists(), 'The real command must start before the outage'
                pids.extend(effects.read_text().splitlines())
                state['failed_at'] = time.monotonic()
                on_outage()
                progress('Injected HTTP 502 while the original command is running.')
                return self.reply({'error': {'type': 'api_error', 'message': 'broker restarting'}}, 502)
            else:
                assert correction in json.dumps(data['messages']), 'First resumed inference must receive the correction'
                if not any('original-command-completed' in str(m.get('content')) for m in data['messages'] if m['role'] == 'user'):
                    block = {'type': 'tool_use', 'id': 'inspect_original', 'name': 'Bash', 'input': {
                        'command': 'while [ ! -f ' + shlex.quote(str(completed)) + ' ]; do sleep 0.1; done; cat ' + shlex.quote(str(completed)),
                        'timeout': 5000, 'description': 'Inspect the original command receipt'}}
                else:
                    block = {'type': 'text', 'text': 'Original command completed once. Recovery verified.'}
            send_message(self, data, block, {'input_tokens': 100, 'output_tokens': 20})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'sdk-config'))
    store = ContextStore(tmp_path / 'context.sqlite3', 'claude-live-recovery')
    store.initialize([])
    def report(kind, message, data):
        events.append({'kind': kind, 'message': message, 'data': data})
        if on_status:
            on_status(kind, message, data)
    reporter = ActivityReporter(report)
    relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', capability, report_error=diagnostics.append).start()
    agent = ClaudeAgent(spec={'model': 'fireworks_ai/glm-5p3', 'timeout': outage_seconds + 40,
                             'max_iterations': 8, 'transport_recovery_seconds': outage_seconds + 10},
        relay=relay, config={'mcp_servers': {'workspace': {}}}, activity=reporter,
        step=lambda: None, cwd=str(tmp_path), definition=None, context_store=store)
    if delay_continuation or late_blocked_result:
        query, receive, before_model = ClaudeSDKClient.query, ClaudeSDKClient.receive_messages, relay.before_model
        continued = asyncio.Event()
        async def delayed_query(client, prompt, **kwargs):
            continuation = isinstance(prompt, str) and 'Recovery input:' in prompt
            if continuation and delay_continuation:
                await asyncio.sleep(1)
            result = await query(client, prompt, **kwargs)
            if continuation:
                continued.set()
            return result
        async def delayed_receive(client):
            async for message in receive(client):
                if (late_blocked_result and isinstance(message, ResultMessage) and message.is_error
                        and message.api_error_status in {400, 409}):
                    assert relay.model_failed and not continued.is_set()
                    progress('Command finished during the outage; holding its rejected-turn reply.')
                    await continued.wait()
                    await asyncio.sleep(.05)  # Deliver after the recovery coroutine has returned.
                    state['late_result'] = True
                    progress('Delivering the earlier rejection after reconnection.')
                yield message
        def admission(request):
            try:
                return before_model(request)
            except InputPending:
                state['blocked'] += 1
                raise
        monkeypatch.setattr(ClaudeSDKClient, 'query', delayed_query)
        monkeypatch.setattr(ClaudeSDKClient, 'receive_messages', delayed_receive)
        relay.before_model = admission
    options = agent.options
    native_sessions = []
    def native_options(system):
        configured = replace(options(system), tools=['Bash'], allowed_tools=['Bash'], mcp_servers={})
        native_sessions.append(configured.session_id)
        return configured
    agent.options = native_options
    try:
        result = agent.run_conversation('Run the original command exactly once and inspect its result.',
            conversation_history=[], system_message='Local recovery verification. Do not duplicate a command.')
        progress(json.dumps({'completed': result['completed'], 'pending': sorted(agent.journal.pending),
            'sessions': native_sessions, 'attempts': getattr(agent, 'transport_attempt', 0), 'answer': result['final_response']}))
        assert result['completed'], result
        assert effects.read_text().splitlines() == pids and len(pids) == 1
        assert completed.read_text() == 'original-command-completed'
        assert len(native_sessions) == 1, 'Recovery must keep the native process/session alive'
        assert agent.transport_attempt == 1
        if delay_continuation:
            assert state['blocked'] > 0, 'The stale automatic turn must be rejected before inference'
        if late_blocked_result:
            assert state['late_result'], 'The outage-time rejection must arrive after recovery has finished'
        assert len(diagnostics) == 1 and diagnostics[0]['http_status'] == 502
        assert not agent.journal.pending and not store.pending
        assert sum(m.get('content') == '[User correction to the current task]\n' + correction for m in agent.journal.messages) == 1
        assert [e['data']['phase'] for e in events if e['data'].get('stage') == 'model_transport'] == ['reconnecting', 'recovered']
        assert len([m for m in agent.journal.messages if m.get('tool_calls') and m['tool_calls'][0]['id'] == 'original_command']) == 1
        return {'completed': True, 'native_sessions': len(native_sessions), 'original_command_executions': len(pids),
                'recovery_attempts': agent.transport_attempt, 'readiness_probes': state['probes'],
                'outage_elapsed': state['outage_elapsed'], 'original_pids': pids,
                'pending_tools': len(agent.journal.pending), 'answer': result['final_response'], 'events': events}
    finally:
        ready.touch()
        agent.close()
        relay.close()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('race', ['normal', 'background-completion', 'late-blocked-result'])
def test_real_claude_session_survives_broker_outage(tmp_path, monkeypatch, race):
    live_recovery_case(tmp_path, monkeypatch, delay_continuation=race == 'background-completion',
                       late_blocked_result=race == 'late-blocked-result')
