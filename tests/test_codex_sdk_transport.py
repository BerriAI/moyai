"""Published Codex runtime, real native/MCP tools, and synthetic Responses inference."""
import json
import re
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from sandbox.codex_harness import CodexAgent
from sandbox.broker_relay import BrokerRelay
from sandbox.context_store import ContextStore, ContextUnavailable
from test_workspace import workspace as broker_workspace


def send_response(handler, output, sequence, input_tokens=500, *, interrupted=None):
    response = {'id': f'resp_fixture_{sequence}', 'status': 'completed', 'output': [output],
                'usage': {'input_tokens': input_tokens, 'output_tokens': 30, 'total_tokens': input_tokens + 30}}
    frames = [
        {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': output},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': output},
        {'type': 'response.completed', 'response': response},
    ]
    if interrupted:
        frames.pop()
    raw = ''.join('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n'
                  for frame in frames).encode()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Content-Length', str(len(raw) + (64 if interrupted == 'truncated' else 0)))
    handler.end_headers()
    handler.wfile.write(raw)
    handler.wfile.flush()
    if interrupted:
        handler.close_connection = True


def background_gateway(tmp_path, monkeypatch, broker_workspace, harness, on_upstream,
                       progress=lambda text: None):
    """Real broker/relay and a delayed provider, shared by installed SDK proofs."""
    import asyncio
    from contextlib import contextmanager
    import httpx
    from app.context_budget import ModelContextLimits
    from app.context_compaction import INSTRUCTIONS, PRIVATE_INSTRUCTIONS
    from app.security import digest
    from sandbox.broker_transport import unseal

    @contextmanager
    def running():
        app, client = broker_workspace
        model = 'anthropic/claude-opus-5-5' if harness == 'claude-agent-sdk' else 'openai/gpt-6-astra'
        capability = 'background-fixture-capability'
        app.state.settings.agent_model = model
        app.state.settings.litellm_api_base = 'http://background-provider.test'
        app.state.settings.max_agent_iterations = 100
        run = app.state.store.create_run('Continue each tool once.', '', 'modal', [],
            harness=harness, model=model, chat_enabled=True, user_id='google:background-fixture')
        app.state.store.claim_message(run['id'])
        app.state.store.update_run(run['id'], status='running', token_hash=digest(capability))
        async def limits(model):
            window = 280000 if harness == 'claude-agent-sdk' else 200000
            return ModelContextLimits(context_window=window, max_input_tokens=window,
                                      max_output_tokens=128000, default_output_tokens=4096)
        monkeypatch.setattr(app.state.context_budget, 'limits', limits)
        state = SimpleNamespace(app=app, run=run, requests=[], original=[], summaries=[],
            service_requests=[], counter_requests=[], public_summaries=0,
            held_calls=0, projections=[], summary_started=threading.Event(),
            release=threading.Event(), summary_done=False, tail_marker='', faults=[],
            summary_marker='private-background-summary-marker', message_seq=0)

        async def upstream(request):
            state.service_requests.append(request.url.path)
            body = json.loads(request.content)
            if request.url.path == '/utils/token_counter':
                state.counter_requests.append(body)
                count = sum(len(block['text'].encode()) for message in body['messages']
                    for block in message['content'] if block['type'] == 'text')
                return httpx.Response(200, json={'total_tokens': max(1, count),
                                                'tokenizer_type': 'fixture_utf8'})
            assert request.url.path in {'/v1/messages', '/v1/responses',
                                         '/v1/chat/completions', '/chat/completions'}
            instructions = body.get('messages', [{}])[0].get('content', '')
            if isinstance(instructions, str) and instructions.startswith(PRIVATE_INSTRUCTIONS):
                state.summaries.append(body)
                if len(state.summaries) == 1:
                    state.summary_started.set()
                    progress('Background summary started; foreground inference remains available.')
                    try:
                        async with asyncio.timeout(20):
                            while not state.release.is_set():
                                await asyncio.sleep(0.005)
                    finally:
                        state.summary_done = True
                source = json.dumps(body)
                sessions = re.findall(r'session_id\\*"\s*:\s*(\d+)', source)
                summary = state.summary_marker + ' Preserve the task and completed tools. '
                if sessions:
                    summary += 'The original command is still running: ' + json.dumps({'session_id': int(sessions[0])})
                if state.tail_marker and state.tail_marker in source:
                    summary += ' ' + state.tail_marker
                return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': summary}}],
                    'usage': {'prompt_tokens': 500, 'completion_tokens': 50, 'total_tokens': 550}})
            if isinstance(instructions, str) and instructions.startswith(INSTRUCTIONS):
                state.public_summaries += 1
                return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'Completed fixture tools remain in their saved receipts.'}}],
                    'usage': {'prompt_tokens': 500, 'completion_tokens': 20, 'total_tokens': 520}})
            state.requests.append(body)
            if state.summary_started.is_set() and not state.summary_done:
                state.held_calls += 1
                if not state.tail_marker:
                    state.tail_marker = 'tail-added-while-summarizing'
                progress(f'Foreground request {len(state.requests)} proceeds while summary is held.')
                if state.held_calls >= 2:
                    state.release.set()
            text = json.dumps(body)
            if state.summary_marker in text:
                assert state.tail_marker in text, 'Installing the prefix summary discarded the new tail'
                state.projections.append(body)
            return on_upstream(body, state)

        original_init = httpx.AsyncClient.__init__
        def initialize_client(self, *args, **kwargs):
            kwargs['mounts'] = {**(kwargs.get('mounts') or {}),
                               'http://background-provider.test': httpx.MockTransport(upstream)}
            original_init(self, *args, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, '__init__', initialize_client)

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def forward(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path in {'/v1/responses', '/v1/messages', '/v1/chat/completions'}:
                    state.original.append(json.loads(unseal(capability, self.path, raw)))
                headers = {name: self.headers[name] for name in
                    ('Authorization', 'Content-Type', 'anthropic-version', 'anthropic-beta') if name in self.headers}
                response = client.request(self.command, '/broker/' + run['id'] + self.path,
                                          content=raw, headers=headers)
                if response.status_code >= 400:
                    progress(f'Fixture broker {self.path}: HTTP {response.status_code} {response.text[:250]}')
                    if state.original:
                        progress('Requested output allowance: ' + str(state.original[-1].get('max_tokens')))
                self.send_response(response.status_code)
                for name, value in response.headers.items():
                    if name.lower() not in {'content-length', 'connection', 'transfer-encoding'}:
                        self.send_header(name, value)
                self.send_header('Content-Length', str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            do_GET = do_POST = forward

        server = ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
        relay = BrokerRelay(f'http://127.0.0.1:{server.server_port}', capability,
                            report_error=state.faults.append).start()
        state.relay, state.capability = relay, capability
        try:
            yield state
        finally:
            state.release.set()
            relay.close()
            server.shutdown()
            server.server_close()
    return running()


def native_background_case(tmp_path, monkeypatch, broker_workspace, harness, progress=lambda text: None,
                           *, expect_background=True):
    """A held summary overlaps real native tools without replacing their runtime."""
    from dataclasses import replace
    from io import BytesIO
    import httpx
    from sandbox.claude_harness import ClaudeAgent
    from test_claude_native_compaction import send_message

    workspace = tmp_path / 'native-tools'
    workspace.mkdir()
    executions = workspace / 'executions.txt'
    (workspace / 'held.py').write_text(
        'import pathlib,time\n'
        'with pathlib.Path("executions.txt").open("a") as f: f.write("held\\n")\n'
        'while not pathlib.Path("release").exists(): time.sleep(0.02)\n'
        'print("original-command-finished", flush=True)\n')
    events, sessions = [], []
    command_session = None
    codex = harness == 'codex'
    tool_steps = 16 if codex else 24
    # Each Codex receipt includes the command input and output. Keep those
    # increments small enough for two more calls inside the 25% headroom.
    # Claude's native Read truncates long lines; give it enough real reads
    # to exercise the projected history after crossing the same threshold.
    receipt_repeats = 2500 if codex else 4500

    def upstream(body, state):
        nonlocal command_session
        sequence = len(state.requests)
        pipe = SimpleNamespace(server=state, wfile=BytesIO(), send_response=lambda *a: None,
            send_header=lambda *a: None, end_headers=lambda: None)
        text = json.dumps(body)
        if codex:
            sessions.append(state.original[-1]['client_metadata']['thread_id'])
            handles = re.findall(r'session_id\\*"\s*:\s*(\d+)', text)
            if handles and command_session is None:
                command_session = int(handles[0])
            if sequence == 1:
                code = 'text(await tools.exec_command(' + json.dumps({
                    'cmd': shlex.quote(sys.executable) + ' held.py', 'tty': True,
                    'login': False, 'yield_time_ms': 1000}) + '));'
            elif sequence <= tool_steps + 1:
                step = sequence - 1
                script = ('import pathlib; '
                    f'pathlib.Path("executions.txt").open("a").write("step-{step}\\n"); '
                    + 'print(' + repr(f'receipt-{step} ' + state.tail_marker + ' x' * receipt_repeats) + ')')
                code = 'text(await tools.exec_command(' + json.dumps({
                    'cmd': shlex.quote(sys.executable) + ' -c ' + shlex.quote(script),
                    'login': False, 'yield_time_ms': 1000, 'max_output_tokens': 5000}) + '));'
            elif sequence == tool_steps + 2:
                assert (state.projections or not expect_background) and command_session in [int(value) for value in handles]
                (workspace / 'release').touch()
                code = 'text(await tools.write_stdin(' + json.dumps({
                    'session_id': command_session, 'chars': '', 'yield_time_ms': 1000}) + '));'
            else:
                assert sequence == tool_steps + 3 and 'original-command-finished' in text
                code = None
            block = ({'type': 'custom_tool_call', 'id': f'code_{sequence}', 'call_id': f'code_{sequence}',
                'name': 'exec', 'namespace': 'functions', 'input': code} if code else
                {'type': 'message', 'id': 'final', 'role': 'assistant', 'phase': 'final_answer',
                 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'background-native-ok'}]})
            send_response(pipe, block, sequence, input_tokens=len(text) // 4)
        else:
            sessions.append(json.loads(state.original[-1]['metadata']['user_id'])['session_id'])
            if sequence <= tool_steps:
                receipt = workspace / f'receipt-{sequence}.txt'
                receipt.write_text(f'receipt-{sequence} ' + state.tail_marker + ' x' * receipt_repeats)
                block = {'type': 'tool_use', 'id': f'read_{sequence}', 'name': 'Read',
                         'input': {'file_path': str(receipt)}}
            else:
                assert sequence == tool_steps + 1 and (state.projections or not expect_background)
                block = {'type': 'text', 'text': 'background-native-ok'}
            send_message(pipe, body, block, {'input_tokens': len(text) // 4, 'output_tokens': 100})
        return httpx.Response(200, content=pipe.wfile.getvalue(), headers={'Content-Type': 'text/event-stream'})

    with background_gateway(tmp_path, monkeypatch, broker_workspace, harness, upstream, progress) as state:
        store = ContextStore(tmp_path / 'native-context.sqlite3', state.run['id'])
        store.initialize([])
        cls = CodexAgent if codex else ClaudeAgent
        agent = cls(spec={'model': state.app.state.settings.agent_model, 'timeout': 60,
            'max_iterations': tool_steps + 10},
            relay=state.relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
                'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
                'env': {'WORKSPACE_BROKER_URL': state.relay.url, 'WORKSPACE_RUN_TOKEN': state.capability}}}},
            activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
                complete=lambda *args: events.append(('complete', args)), commentary=progress),
            step=lambda: None, cwd=str(workspace), definition=None, context_store=store)
        if not codex:
            options = agent.options
            agent.options = lambda system: replace(options(system), tools=['Read'], allowed_tools=['Read'], mcp_servers={})
        try:
            from sandbox.context_store import ContextUnavailable
            try:
                result = agent.run_conversation('Perform the fixture steps once and preserve the current task.',
                    conversation_history=[], system_message='Keep each tool receipt. Do not repeat completed actions.')
            except ContextUnavailable as exc:
                if expect_background:
                    raise
                result = {'completed': False, 'final_response': str(exc)}
            if not expect_background:
                assert not result['completed'], 'This baseline completed; do not label it a reproduced failure'
                assert not state.summaries and not state.held_calls and not state.projections
                pending = len(store.pending)
                progress(f'BASELINE {harness}: {len(state.requests)} foreground calls, '
                         f'{len(state.service_requests)} total provider requests, {pending} pending tool(s); '
                         + result['final_response'])
                return {'completed': False, 'foreground_calls': len(state.requests), 'overlap': 0,
                        'provider_calls': len(state.service_requests), 'pending': pending,
                        'failure': result['final_response'], 'projections': 0}
            assert result['completed'], (result['final_response'], state.faults, len(state.requests), len(state.summaries))
            assert result['final_response'] == 'background-native-ok'
            assert state.held_calls >= 2 and state.projections and not state.faults
            assert len(state.original) == len(state.requests) == tool_steps + (3 if codex else 1)
            assert len(state.service_requests) == (len(state.requests) + len(state.summaries)
                                                   + state.public_summaries + len(state.counter_requests))
            assert len(set(sessions)) == 1, 'Compaction replaced the native runtime'
            assert not store.pending
            started = [args[0] for kind, args in events if kind == 'start']
            completed = [args[0] for kind, args in events if kind == 'complete']
            assert len(started) == len(set(started)) == tool_steps + int(codex)
            assert sorted(started) == sorted(completed)
            assert state.tail_marker not in json.dumps(state.summaries[0])
            assert state.summary_marker not in json.dumps(state.original)
            assert state.summary_marker not in ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
            if codex:
                assert 'You are performing a CONTEXT CHECKPOINT COMPACTION.' not in json.dumps(state.original)
                assert executions.read_text().splitlines() == ['held', *[f'step-{i}' for i in range(1, tool_steps + 1)]]
            else:
                assert agent.native_compactions == 0
            progress(f'PASS {harness}: {len(state.requests)} foreground calls, {state.held_calls} while summary held; '
                     f'{len(state.service_requests)} total provider requests; {len(completed)} receipts, '
                     'one native session, preserved new tail.')
            return {'foreground_calls': len(state.requests), 'overlap': state.held_calls,
                    'provider_calls': len(state.service_requests),
                    'receipts': len(completed), 'projections': len(state.projections)}
        finally:
            (workspace / 'release').touch()
            agent.close()
            store.close()


def test_codex_background_compaction_preserves_running_command(tmp_path, monkeypatch, broker_workspace):
    native_background_case(tmp_path, monkeypatch, broker_workspace, 'codex', progress=print)


@pytest.mark.parametrize('outcome', [
    'complete', 'unphased', 'interrupt', 'provider-error', 'compaction', 'context-recovery-limit', 'iteration-limit'])
def test_native_astra_tools_checkpoint_and_fresh_context(tmp_path, monkeypatch, outcome):
    requests, attempts, calls, events, faults = [], [], [], [], []
    samples, summaries = [], []
    boundaries, recovered = [], []
    successful = outcome in {'complete', 'unphased', 'compaction', 'context-recovery-limit'}
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
                if outcome == 'unphased':
                    output.pop('phase')
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
        assert ('commentary', 'codex-transport-ok') not in events
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
        if outcome == 'unphased':
            agent.context.spec['model'] = 'anthropic/claude-opus-5-5'
        result = agent.run_conversation('Continue using the saved receipts.', conversation_history=[],
                                        system_message='requester-private-second-marker')
        if outcome == 'unphased':
            assert requests[-1]['model'] == 'anthropic/claude-opus-5-5'
            assert result['final_response'] == 'codex-transport-ok'
            assert ('commentary', 'codex-transport-ok') not in events
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


@pytest.mark.parametrize('outcome,delay,rounds,rejections', [
    ('settle-context', 12, 1, 0), ('settle-context-preview', 0, 2, 1),
    ('settle-context-during-compaction', 0, 1, 0), ('settle-context-transport-preview', 0, 1, 0)])
def test_context_rejection_compacts_in_place_with_live_tools(tmp_path, monkeypatch, outcome, delay, rounds, rejections):
    proof = native_yield_case(tmp_path, monkeypatch, 1000, outcome,
                             context_delay=delay, context_rounds=rounds, compact_rejections=rejections)
    assert proof['completed'] and not proof['pending_tools'], proof
    assert proof['tool_executions'] == proof['completed_receipts'] == 1
    assert proof['native_clients'] == proof['native_threads'] == 1
    assert proof['native_compactions'] == rejections + rounds
    transport_retry = int(outcome == 'settle-context-transport-preview')
    assert proof['transport_attempt'] == transport_retry
    assert proof['upstream_requests'] == proof['model_calls'] == 3 + 2 * rounds + rejections + transport_retry
    assert proof['corrections_delivered'] == int(rounds > 1)
    assert proof['receipt_during_compaction'] == (outcome == 'settle-context-during-compaction')
    assert 'private-native-summary-marker' not in proof['saved_prose']
    assert proof['final_response'] == 'yield-test-complete'


@pytest.mark.parametrize('outcome', ['settle-transport-finite', 'settle-transport-preview'])
def test_native_transport_recovery_preserves_running_commands(tmp_path, monkeypatch, outcome):
    proof = native_yield_case(tmp_path, monkeypatch, 1000, outcome)
    assert proof['completed'] and not proof['pending_tools'], proof
    assert proof['native_clients'] == proof['native_threads'] == 1
    assert proof['tool_executions'] == proof['completed_receipts'] == 1
    assert proof['upstream_requests'] == 4 and proof['transport_attempt'] == 1
    assert len(proof['transport_errors']) == 1
    assert proof['transport_errors'][0]['http_status'] == 502
    assert proof['transport_errors'][0]['response_started'] is False
    assert proof['final_response'] == 'yield-test-complete'


def test_native_safe_read_reconnects_without_replaying_completed_write(tmp_path, monkeypatch):
    proof = native_yield_case(tmp_path, monkeypatch, 2000, 'read-outage')
    assert proof['completed'] and not proof['pending_tools'], proof
    assert not proof['faults'] and not proof['transport_errors']
    assert proof['native_clients'] == proof['native_threads'] == 1
    assert proof['tool_executions'] == proof['completed_receipts'] == 2
    assert proof['read_attempts'] == 2 and proof['upstream_requests'] == 3
    assert (tmp_path / 'workspace' / 'writes').read_text() == 'once\n'
    assert proof['final_response'] == 'yield-test-complete'


def test_native_redeploy_outage_preserves_preview(tmp_path, monkeypatch):
    proof = native_yield_case(tmp_path, monkeypatch, 1000, 'settle-transport-preview',
                             outage_seconds=40, progress=print)
    assert proof['completed'] and not proof['pending_tools'], proof
    assert proof['native_clients'] == proof['native_threads'] == 1
    assert proof['tool_executions'] == proof['completed_receipts'] == proof['command_starts'] == 1
    assert proof['upstream_requests'] == proof['model_calls'] == 4
    assert proof['transport_attempt'] == 1
    assert proof['outage_elapsed'] >= 40
    assert proof['readiness_statuses'][0] == 503 and proof['readiness_statuses'][-1] == 200
    assert {sample['phase'] for sample in proof['preview_samples']} == {'before', 'during', 'after'}
    assert len({(sample['pid'], sample['port']) for sample in proof['preview_samples']}) == 1
    assert all(sample['status'] == 200 for sample in proof['preview_samples'])
    assert not proof['faults'] and proof['saved_prose'].count('yield-test-complete') == 1


@pytest.mark.parametrize('interrupted', ['clean', 'truncated'])
def test_native_interrupted_sse_preserves_existing_command(tmp_path, monkeypatch, interrupted):
    proof = native_yield_case(tmp_path, monkeypatch, 1000, 'settle-transport-preview',
                             interrupted=interrupted, progress=print)
    assert proof['completed'] and not proof['pending_tools'], proof
    assert proof['native_clients'] == proof['native_threads'] == 1
    assert proof['tool_executions'] == proof['completed_receipts'] == proof['command_starts'] == 1
    assert proof['upstream_requests'] == proof['model_calls'] == 3
    assert proof['transport_attempt'] == 1
    assert len(proof['transport_errors']) == 1
    assert proof['transport_errors'][0]['response_started'] is True
    assert not proof['faults'] and proof['saved_prose'].count('yield-test-complete') == 1


def native_yield_case(tmp_path, monkeypatch, yield_ms, outcome='complete', *,
                      agent_class=CodexAgent, progress=lambda text: None, context_delay=0.3,
                      context_rounds=1, compact_rejections=0,
                      outage_seconds=0, interrupted=None, on_outage=lambda: None,
                      on_status=lambda kind, message, data: None):
    """Real Codex + MCP + relay, including the default ~30-second exec yield.

    The tool waits for the next model request to reach the upstream server.
    Requiring nested completion before admitting that request deadlocks.
    """
    from sandbox.broker_relay import BrokerRelay
    from sandbox.broker_transport import unseal
    capability = 'yield-fixture-capability'
    release = threading.Event()
    calls, requests, steps, events, faults, diagnostics, read_attempts = [], [], [], [], [], [], []
    outage_started = outage_until = 0
    readiness_statuses, preview_samples, status_events = [], [], []
    stop_observing = threading.Event()
    observer = None
    settlement_sent = False
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    settlement = outcome.startswith('settle-')
    preview = outcome.endswith('preview')
    transport = outcome.startswith('settle-transport-') or outcome == 'settle-context-transport-preview'
    context_case = outcome.startswith('settle-context')
    finish_during_compaction = outcome == 'settle-context-during-compaction'
    read_outage = outcome == 'read-outage'
    receipt_during_compaction = threading.Event()
    compactions = []
    corrections = []
    if settlement:
        (workspace / 'command.py').write_text(
            'import http.server, os, pathlib, signal, sys, time\n'
            'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n'
            'with pathlib.Path("command-starts").open("a") as starts: starts.write(str(os.getpid()) + "\\n")\n'
            'try:\n' + (
                ' server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)\n'
                ' pathlib.Path("preview-port").write_text(str(server.server_port))\n'
                ' pathlib.Path("preview-pid").write_text(str(os.getpid()))\n'
                ' print("preview-ready", flush=True)\n server.serve_forever()\n'
                if preview else
                ' while not pathlib.Path("release").exists(): time.sleep(0.05)\n'
                + (f' time.sleep({context_delay})\n' if context_case else '')) +
            'finally:\n print("slow-tool-complete", flush=True)\n')

    def sample_preview(phase):
        from urllib.request import urlopen
        try:
            port = int((workspace / 'preview-port').read_text())
            pid = int((workspace / 'preview-pid').read_text())
            with urlopen(f'http://127.0.0.1:{port}/command.py', timeout=2) as response:
                sample = {'phase': phase, 'pid': pid, 'port': port, 'status': response.status}
            preview_samples.append(sample)
            return sample
        except (OSError, ValueError) as exc:
            faults.append(f'Original preview unavailable {phase} recovery: {type(exc).__name__}')

    def observe_outage():
        while not stop_observing.wait(1) and time.monotonic() < outage_until:
            sample_preview('during')
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
    agent = None
    native_errors, native_error_payloads, native_plugins, native_clients, native_threads = [], [], [], [], []
    from openai_codex.async_client import AsyncCodexClient
    from sandbox.sdk_failure import codex_details
    class ObservedClient(AsyncCodexClient):
        async def initialize(self):
            native_clients.append(self)
            from openai_codex.generated.v2_all import ExperimentalFeatureListResponse
            result = await super().initialize()
            features = await self.request('experimentalFeature/list', {},
                                         response_model=ExperimentalFeatureListResponse)
            native_plugins.extend(feature.enabled for feature in features.data if feature.name == 'plugins')
            progress('Native plugin marketplace enabled: ' + json.dumps(native_plugins))
            return result

        async def thread_start(self, options):
            result = await super().thread_start(options)
            native_threads.append(result.thread.id)
            return result

        async def next_turn_notification(self, turn_id):
            event = await super().next_turn_notification(turn_id)
            if (context_case and event.method == 'turn/completed'
                    and agent.context.relay.context_required and not finish_during_compaction):
                # Finish only after the failed turn: recovery must keep the
                # original runtime alive to obtain this command's receipt.
                (workspace / 'release').touch()
            if event.method == 'error':
                payload = event.payload.model_dump(mode='json', by_alias=True)
                native_error_payloads.append(payload)
                if interrupted:
                    progress('Synthetic fixture native error payload: ' + json.dumps(payload))
                detail = codex_details(payload.get('error'), will_retry=payload.get('willRetry'))
                native_errors.append(detail)
                progress('Native SDK error: ' + json.dumps(detail))
            return event
    monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', ObservedClient)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            if status == 409:
                self.send_header('X-Moyai-Context', 'compact')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass  # The baseline SDK exits with the fixture tool unresolved.

        def do_GET(self):
            assert self.headers['Authorization'] == 'Bearer ' + capability
            status = 503 if time.monotonic() < outage_until else 200
            if self.path == '/v1/models':
                readiness_statuses.append(status)
                progress(f'Authenticated broker readiness: HTTP {status}')
            if status == 503:
                return self.reply({}, status)
            if self.path == '/v1/models':
                return self.reply({'object': 'list', 'data': [{'id': 'openai/gpt-6-astra'}]})
            if self.path == '/context/window':
                return self.reply({'input_budget': 200000})
            assert self.path == '/tools'
            self.reply([{'name': 'slow_echo', 'description': 'Wait for a local fixture signal.',
                **({'annotations': {'readOnlyHint': True, 'idempotentHint': True}} if read_outage else {}),
                'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}])

        def do_POST(self):
            nonlocal outage_started, outage_until, observer, settlement_sent
            assert self.headers['Authorization'] == 'Bearer ' + capability
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(unseal(capability, self.path, raw))
            if time.monotonic() < outage_until:
                if self.path == '/v1/responses':
                    requests.append(body)
                return self.reply({}, 503)
            if self.path == '/context/maintenance':
                if agent.journal is not None:
                    assert not agent.journal.pending
                return self.reply({})
            if self.path == '/tools/call':
                read_attempts.append(body)
                if read_outage and len(read_attempts) == 1:
                    return self.reply({}, 502)
                calls.append(body)
                progress('Tool started; it waits for an admitted model poll')
                if not release.wait(timeout=50):
                    faults.append('Model polling was blocked while the tool was running')
                return self.reply({'text': 'slow-tool-complete'})
            assert self.path == '/v1/responses'
            requests.append(body)
            if context_case and relay.native_compacting:
                compactions.append(body)
                progress('Native compaction request; original command remains live')
                assert agent.journal.pending
                if len(compactions) <= compact_rejections:
                    return self.reply({'detail': {'code': 'context_compaction_required',
                        'input_tokens': 300000, 'input_budget': 200000}}, 409)
                if len(compactions) == compact_rejections + 1 and compact_rejections:
                    assert len(json.dumps(body)) < len(json.dumps(compactions[0]))
                if context_rounds > 1 and len(compactions) == compact_rejections + context_rounds:
                    assert agent.accept_input({'id': 42, 'content': 'Keep the final response concise.'})
                if preview:
                    from urllib.request import urlopen
                    port = (workspace / 'preview-port').read_text()
                    with urlopen(f'http://127.0.0.1:{port}/command.py', timeout=2) as response:
                        assert response.status == 200
                    progress('Original preview still serves HTTP 200 during compaction')
                sessions = re.findall(r'session_id(?:\\?"\s*:\s*|:\s*)(\d+)', json.dumps(body))
                assert sessions, 'Native summary must retain the running command handle'
                if finish_during_compaction:
                    (workspace / 'release').touch()
                    assert receipt_during_compaction.wait(5), 'Receipt must be saved while compaction is still running'
                    assert not agent.journal.pending and agent.journal.completed_tools == 1
                    progress('Original command receipt saved before the compaction response')
                output = {'type': 'message', 'id': 'native-summary', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text':
                    'private-native-summary-marker. A command is still running, session_id: ' + sessions[-1]
                    + '. Collect its result using write_stdin; do not rerun it.'}]}
                return send_response(self, output, len(requests))
            sequence = len(requests) - len(compactions)
            progress(f'Model request {sequence} admitted; pending tools: {len(agent.journal.pending)}')
            if sequence == 1:
                code = ('text(await tools.exec_command(' + json.dumps({
                    'cmd': shlex.quote(sys.executable) + ' command.py', 'tty': True,
                    'login': False, 'yield_time_ms': 1000}) + '));' if settlement else
                    'text(await tools.mcp__moyai__slow_echo({}));')
                if read_outage:
                    code = 'text(await tools.exec_command({cmd: "printf \'once\\n\' >> writes; printf write-receipt", login: false}));' + code
                if yield_ms is not None and not settlement:
                    code = '// @exec: ' + json.dumps({'yield_time_ms': yield_ms}) + '\n' + code
                output = {'type': 'custom_tool_call', 'id': 'exec_1', 'call_id': 'outer_1',
                          'name': 'exec', 'namespace': 'functions', 'input': code}
                if interrupted:
                    progress('Interrupting SSE after the command output item, before response.completed')
                    return send_response(self, output, sequence, interrupted=interrupted)
            elif transport and sequence == 2 and not interrupted:
                assert agent.journal.pending and steps == [0]
                if outage_seconds:
                    sample_preview('before')
                    outage_started = time.monotonic()
                    outage_until = outage_started + outage_seconds
                    observer = threading.Thread(target=observe_outage, daemon=True)
                    observer.start()
                    progress(f'Broker unavailable for {outage_seconds} seconds; original preview stays running')
                    on_outage()
                    return self.reply({}, 503)
                progress('Injecting an edge HTTP 502 with the existing command still running')
                return self.reply({}, 502)
            elif context_case and 2 + int(transport) <= sequence <= context_rounds + 1 + int(transport):
                assert agent.journal.pending and steps == [0]
                return self.reply({'detail': {'code': 'context_compaction_required',
                    'input_tokens': 300000, 'input_budget': 200000}}, 409)
            elif settlement and sequence == 2 and not interrupted:
                assert agent.journal.pending and steps == [0]
                if preview:
                    from urllib.request import urlopen
                    port = (workspace / 'preview-port').read_text()
                    with urlopen(f'http://127.0.0.1:{port}/command.py', timeout=2) as response:
                        assert response.status == 200
                    progress('Preview server answered HTTP 200; model prematurely finishes')
                output = {'type': 'message', 'id': 'premature', 'role': 'assistant',
                    'phase': 'final_answer', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'premature-answer'}]}
            elif (settlement and not settlement_sent and sequence >=
                    (context_rounds + 2 + int(transport) if context_case else 2 if interrupted else 3)):
                assert bool(agent.journal.pending) != finish_during_compaction
                assert agent.model_calls == sequence + len(compactions)
                if transport and preview:
                    sample_preview('after')
                    progress('After reconnect: original preview server still answers HTTP 200')
                corrections.extend(item for item in body['input'] if 'Keep the final response concise.' in json.dumps(item))
                sessions = re.findall(r'session_id(?:\\?"\s*:\s*|:\s*)(\d+)', json.dumps(body))
                assert sessions, 'Native command must return a running session'
                (workspace / 'release').touch()
                progress('Settlement turn stops the preview or waits for finite work')
                output = {'type': 'custom_tool_call', 'id': 'settle', 'call_id': 'settle',
                    'name': 'exec', 'namespace': 'functions',
                    'input': 'text(await tools.write_stdin(' + json.dumps({
                        'session_id': int(sessions[-1]), 'yield_time_ms': 20000 if context_case else 1000,
                        'chars': '\u0003' if preview else ''}) + '));'}
                settlement_sent = True
            elif sequence == 2 and not settlement:
                if read_outage and not calls:
                    faults.append('Safe read returned before recovery')
                    return send_response(self, {'type': 'message', 'id': 'unrecovered', 'role': 'assistant',
                        'phase': 'final_answer', 'status': 'completed', 'content': [
                            {'type': 'output_text', 'text': 'read-outage-unrecovered'}]}, sequence)
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
                    assert not agent.journal.pending and agent.journal.completed_tools == 1 + int(read_outage)
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
    if context_case:
        def compact(previous, entries, **kwargs):
            assert not agent.journal.pending
            assert 'slow-tool-complete' in json.dumps(entries)
            return {'summary': 'The command already ran once: slow-tool-complete. Answer the user.',
                    'through_seq': entries[-1]['seq']}
        relay.compact = compact

    def step():
        assert not agent.journal.pending
        steps.append(agent.journal.completed_tools)
        progress(f'Settled lifecycle check; completed tools: {agent.journal.completed_tools}')
        if outcome == 'interrupt' and agent.journal.completed_tools:
            agent.interrupt()

    def completed(*args):
        events.append(('complete', args))
        if relay.native_compacting:
            receipt_during_compaction.set()

    def emit_status(kind, message, data=None):
        event = {'kind': kind, 'message': message, 'data': data or {}}
        status_events.append(event)
        progress('Status: ' + json.dumps(event))
        on_status(kind, message, data or {})

    agent = agent_class(spec={'model': 'openai/gpt-6-astra', 'timeout': max(60, outage_seconds + 45),
        'max_iterations': 2 if outcome == 'settle-limit' else 8},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
            complete=completed, commentary=lambda text: None,
            emit=emit_status),
        step=step, cwd=str(workspace), definition=None, context_store=store)
    try:
        try:
            result = agent.run_conversation('Run the local slow tool once.', conversation_history=[],
                                            system_message='Local yield regression fixture.')
        except ContextUnavailable as exc:
            result = {'completed': False, 'interrupted': False, 'failed': True,
                      'messages': agent.journal.messages, 'final_response': str(exc)}
        assert calls == ([] if settlement else [{'name': 'slow_echo', 'arguments': {}}])
        receipts = [m for m in result['messages'] if m['role'] == 'tool']
        assert all('slow-tool-complete' in receipt['content'] or
                   (read_outage and 'write-receipt' in receipt['content']) for receipt in receipts)
        proof = {key: result[key] for key in ('completed', 'interrupted', 'failed', 'final_response')}
        proof.update(boundary_failed=agent.boundary_failed, model_calls=agent.model_calls,
            upstream_requests=len(requests), lifecycle_steps=steps,
            tool_executions=sum(kind == 'start' for kind, _ in events),
            completed_receipts=len(receipts), pending_tools=len(store.pending),
            tool_events=[kind for kind, _ in events], faults=faults, transport_errors=diagnostics,
            sdk_failure=result.get('sdk_failure'), native_errors=native_errors, native_plugins=native_plugins,
            native_clients=len(native_clients), native_threads=len(native_threads),
            transport_attempt=agent.transport_attempt, native_compactions=len(compactions),
            read_attempts=len(read_attempts),
            receipt_during_compaction=receipt_during_compaction.is_set(),
            corrections_delivered=len(corrections),
            native_error_payloads=native_error_payloads,
            command_starts=len((workspace / 'command-starts').read_text().splitlines())
                if (workspace / 'command-starts').exists() else 0,
            outage_elapsed=time.monotonic() - outage_started if outage_started else 0,
            readiness_statuses=readiness_statuses, preview_samples=preview_samples, status_events=status_events,
            saved_prose='\n'.join(m.get('content') or '' for m in result['messages'] if m['role'] == 'assistant'))
        progress('Result: ' + json.dumps({key: proof[key] for key in (
            'completed', 'boundary_failed', 'upstream_requests', 'tool_executions', 'completed_receipts', 'pending_tools')}))
        return proof
    finally:
        stop_observing.set()
        if observer:
            observer.join(timeout=3)
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


@pytest.mark.parametrize('harness', ['claude-agent-sdk', 'codex'])
@pytest.mark.parametrize('terminal', ['complete', 'iteration-limit'])
def test_native_automation_validation_can_recover(tmp_path, monkeypatch, broker_workspace, harness, terminal):
    native_automation_validation_case(tmp_path, monkeypatch, broker_workspace, harness, terminal, progress=print)


def native_automation_validation_case(tmp_path, monkeypatch, broker_workspace, harness, terminal,
                                      progress=lambda text: None):
    """Actual app, relay, MCP and native SDK; only inference is scripted."""
    from io import BytesIO
    import httpx
    from sandbox import agent as entrypoint
    from test_automation_tools import definition
    from test_claude_native_compaction import send_message
    from test_spend import sign_in

    app, client = broker_workspace
    sign_in(app, client, 'background-fixture', 'background-fixture@berri.ai')
    codex = harness == 'codex'
    attempts, events, validation_seen = [], [], []
    guest = tmp_path / 'automation-native'
    guest.mkdir()
    mcp_script = str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')
    original_config = entrypoint.hermes_config

    def local_config(spec, broker_url, workspace):
        config = original_config(spec, broker_url, workspace)
        config['mcp_servers']['workspace'].update(command=sys.executable, args=[mcp_script])
        return config

    # Only guest paths and unrelated machine preparation/teardown are replaced.
    # The entrypoint, terminal selection, native runtime and tool path are real.
    monkeypatch.setattr(entrypoint, 'Path', lambda value: guest / str(value).lstrip('/'))
    monkeypatch.setattr(entrypoint, 'hermes_config', local_config)
    monkeypatch.setattr(entrypoint, 'prepare_attachments', lambda *args, **kwargs: None)
    monkeypatch.setattr(entrypoint, 'prepare_project', lambda *args, **kwargs: None)
    monkeypatch.setattr(entrypoint, 'collect_archive', lambda *args: None)
    monkeypatch.setattr(entrypoint, 'computer_request', lambda *args, **kwargs: {})
    monkeypatch.setattr(entrypoint, 'emit', lambda kind, message, data=None, **extra:
        events.append({'kind': kind, 'message': message, 'data': data or {}, **extra}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-config'))

    def upstream(body, state):
        text = json.dumps(body)
        tool = 'mcp__moyai__automation_create'
        discover = not codex and not any(item['name'] == tool for item in body['tools'])
        arguments = None
        if not discover and len(attempts) < 2:
            if attempts:
                validation_seen.append('definition.metadata.bootstrap_source' in text and '16384 characters' in text)
                assert not app.state.store.rows('SELECT * FROM automations')
            arguments = {'turn_id': app.state.store.run(state.run['id'])['active_message_id'],
                'request_key': 'native-corrected-automation', 'definition': definition(**(
                    {} if attempts else {'metadata': {'bootstrap_source': 'private-script-marker' + 'x' * (16385 - len('private-script-marker'))}}))}
            attempts.append('corrected' if attempts else 'invalid')
        sequence = len(state.requests)
        pipe = SimpleNamespace(server=state, wfile=BytesIO(), send_response=lambda *args: None,
                               send_header=lambda *args: None, end_headers=lambda: None)
        if codex:
            output = ({'type': 'custom_tool_call', 'id': f'call_{sequence}', 'call_id': f'call_{sequence}',
                'name': 'exec', 'namespace': 'functions', 'input': 'text(await tools.' + tool + '(' + json.dumps(arguments) + '));'}
                if arguments is not None else {'type': 'message', 'id': 'final', 'role': 'assistant',
                'phase': 'final_answer', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'automation-recovered'}]})
            send_response(pipe, output, sequence)
        else:
            block = ({'type': 'tool_use', 'id': f'find_{sequence}', 'name': 'ToolSearch',
                'input': {'query': 'select:' + tool, 'max_results': 1}} if discover else
                {'type': 'tool_use', 'id': f'call_{sequence}', 'name': tool, 'input': arguments}
                if arguments is not None else {'type': 'text', 'text': 'automation-recovered'})
            send_message(pipe, body, block, {'input_tokens': 500, 'output_tokens': 100})
        return httpx.Response(200, content=pipe.wfile.getvalue(), headers={'Content-Type': 'text/event-stream'})

    with background_gateway(tmp_path, monkeypatch, broker_workspace, harness, upstream, progress) as state:
        exit_code = entrypoint.run_agent({'run_id': state.run['id'], 'harness': harness,
            'broker_url': state.relay.url, 'repo_url': '', 'model': app.state.settings.agent_model,
            'prompt': 'Save the fixture automation, correcting any invalid fields.', 'chat_enabled': True,
            'timeout': 45, 'max_iterations': (2 if codex else 3) if terminal == 'iteration-limit' else 6}, state.relay)
        final = next(event for event in events if event['kind'] == 'final')
        store = ContextStore(guest / 'session/context.sqlite3', state.run['id'])
        try:
            assert len(attempts) == 2
            assert len(app.state.store.rows('SELECT * FROM automations')) == 1
            assert len(app.state.store.rows('SELECT * FROM automation_operations')) == 1
            assert not store.pending
            receipts = [event['data'] for event in events if event['kind'] == 'tool'
                and event['data'].get('tool') == 'automation_create' and event['data'].get('phase') != 'started']
            evidence = {'harness': harness, 'terminal': terminal, 'completed': final['completed'],
                'automation_writes': 1, 'settled_receipts': len(receipts), 'transport_failures': len(state.faults),
                'pending_tools': len(store.pending), 'exit_code': exit_code, 'final_response': final['message']}
            progress(json.dumps(evidence))
            assert [receipt['phase'] for receipt in receipts] == ['error', 'completed']
            # Feed actual native activity to the same projection used by chat,
            # the sidebar and side chats; raw failed receipts remain in events.
            public_run = {'status': 'idle' if final['completed'] else 'failed', 'active_message_id': 1,
                'messages': [{'id': 1, 'role': 'user', 'status': 'completed' if final['completed'] else 'failed'}],
                'events': [{'id': index, 'created_at': '2026-10-09T00:00:00Z', **event,
                            'data': {**event['data'], 'turn_id': 1}} for index, event in enumerate(events, 1)]}
            projection = subprocess.run(['node', '-e',
                "const fs=require('fs'),ui=require(process.argv[1]);const run=JSON.parse(fs.readFileSync(0,'utf8'));"
                "process.stdout.write(JSON.stringify(ui.groups(run).get('1').rows.filter(r=>r.tool==='automation_create').map(r=>r.state)));",
                str(Path(__file__).resolve().parents[1] / 'app/static/activity.js')],
                input=json.dumps(public_run), text=True, capture_output=True, check=True)
            assert json.loads(projection.stdout) == ['completed']

            assert final['completed'] is (terminal == 'complete')
            if terminal == 'complete':
                assert final['message'] == 'automation-recovered' and exit_code == 0
            else:
                assert exit_code == 1 and final['sdk_failure']['pending_tools'] == 0
                assert 'limit' in final['message'] or 'max_turns' in final['message']
                assert 'Invalid automation' not in final['message']
            assert 'transport_failure' not in final and 'transport_retry' not in final
            assert not state.relay.last_error and state.relay.last_failure is None
            assert not state.relay.uncertain_tool and not state.faults
            assert validation_seen == [True]
            return evidence
        finally:
            store.close()
