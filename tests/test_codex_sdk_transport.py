"""Published Codex runtime, real native/MCP tools, and synthetic Responses inference."""
import json
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


@pytest.mark.parametrize('outcome', ['complete', 'interrupt', 'provider-error', 'compaction'])
def test_native_astra_tools_checkpoint_and_fresh_context(tmp_path, monkeypatch, outcome):
    requests, attempts, calls, events, faults = [], [], [], [], []
    samples, summaries = [], []
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
            if not relay.before_model(raw):
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

    def step():
        if outcome == 'interrupt' and agent.journal.completed_tools:
            assert not agent.journal.pending
            agent.interrupt()

    def create_agent():
        return CodexAgent(spec={'model': 'openai/gpt-6-astra', 'timeout': 30, 'max_iterations': 12},
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
        # differ from nested tool receipt IDs; all must settle before the relay
        # allows another inference request or asks Moyai to checkpoint.
        catalog = [tool for body in requests for item in body.get('input', [])
                   if item.get('type') == 'additional_tools' for tool in item['tools']]
        assert any(tool.get('name') == 'functions' for tool in catalog)
        assert not any(tool.get('name') == 'collaboration' for tool in catalog)
        started = [args for kind, args in events if kind == 'start']
        completed = [args for kind, args in events if kind == 'complete']
        assert len(started) == len(completed) == (3 if outcome in {'complete', 'compaction'} else 1)
        assert [args[0] for args in started] == [args[0] for args in completed]
        assert all(args[0] not in {'call_1', 'call_2', 'call_3'} for args in completed)
        assert not agent.journal.pending and not store.pending
        assert 'codex-shell-ok' in json.dumps(result['messages'])
        assert 'provider-private-error-marker' not in json.dumps(result)
        if outcome not in {'complete', 'compaction'}:
            assert not result['completed']
            assert result['interrupted'] is (outcome == 'interrupt')
            assert result['failed'] is (outcome == 'provider-error')
            assert len(requests) == (1 if outcome == 'interrupt' else 2)
            assert len(attempts) == 2
            assert not calls
            return
        assert result['completed'] and result['final_response'] == 'codex-transport-ok'
        assert (workspace / 'receipt.txt').read_text() == 'codex-file-ok\n'
        assert calls == [{'name': 'echo', 'arguments': {'text': 'codex-mcp-ok'}}]
        assert [args[1] for args in completed] == ['terminal', 'apply_patch', 'mcp__moyai__echo']
        if outcome == 'compaction':
            assert len(summaries) >= 2
            assert 'native-private-summary-marker' in json.dumps(samples[-1])
        agent.close()
        store.close()
        store = ContextStore(tmp_path / 'context.sqlite3', 'codex-transport')
        agent = create_agent()
        result = agent.run_conversation('Continue using the saved receipts.', conversation_history=[],
                                        system_message='requester-private-second-marker')
        assert result['completed'] and len(samples) == 5 and len(calls) == 1
        assert len(requests) == 5 + len(summaries)
        assert 'requester-private-first-marker' not in json.dumps(requests[-1])
        assert 'requester-private-second-marker' in json.dumps(requests[-1])
        assert 'codex-mcp-ok' in json.dumps(requests[-1])
        saved = ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
        assert 'requester-private-first-marker' not in saved
        assert 'requester-private-second-marker' not in saved
        assert 'native-private-summary-marker' not in saved
        assert 'native-private-summary-marker' not in json.dumps(requests[-1])
        assert not store.pending
    finally:
        agent.close()
        store.close()
        server.shutdown()
        server.server_close()
