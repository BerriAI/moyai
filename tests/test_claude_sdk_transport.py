"""Real bundled SDK process and real MCP transport; inference is a local fixture.

No provider calls or generated shell commands. Tools read and echo synthetic data.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import shutil
import threading
from types import SimpleNamespace

import pytest

from sandbox.claude_harness import ClaudeAgent
from sandbox.context_store import ContextStore


@pytest.mark.parametrize('resumed', [False, True, 'durable'], ids=['new-session', 'large-checkpoint', 'durable-checkpoint'])
@pytest.mark.parametrize('model', ['anthropic/claude-sonnet-4-5', 'openai/gpt-6-astra', 'fireworks_ai/glm-5p3'])
def test_real_sdk_executes_mcp_and_preserves_receipt(tmp_path, monkeypatch, resumed, model):
    calls, requests, events = [], [], []
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
                calls.append(data)
                return self.reply({'text': data['arguments']['text']})
            assert self.path.startswith('/v1/messages')
            requests.append(data)
            # A fresh SDK session cannot compact an oversized first message.
            if len(json.dumps(data['messages']).encode()) > 100_000:
                return self.reply({'type': 'error', 'error': {'type': 'invalid_request_error',
                    'message': 'Prompt is too long'}}, status=400)
            done = bool(calls)
            receipt = next((block for message in data['messages'] for block in message.get('content', [])
                            if isinstance(block, dict) and block.get('tool_use_id') == 'history_read'), None)
            if resumed == 'durable':
                text = json.dumps(data['messages'])
                assert 'Keep Escape support' in text and 'Do not deploy' in text
                if calls:
                    assert 'sdk-transport-ok' in text
            if resumed is True and receipt is None:
                block = {'type': 'tool_use', 'id': 'history_read', 'name': 'Read', 'input': {
                    'file_path': str(session / '.moyai-history.jsonl'), 'offset': 1, 'limit': 1}}
            else:
                if resumed is True:
                    assert not receipt.get('is_error'), receipt
                    assert 'Continue the searchable dropdown task' in json.dumps(receipt)
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
    workspace, session = tmp_path / 'workspace', tmp_path / 'session'
    workspace.mkdir()
    session.mkdir()
    def create_agent(context_store=None):
        return ClaudeAgent(spec={'model': model, 'timeout': 30, 'max_iterations': 4,
                             'history_reference_dir': str(session)},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': url, 'WORKSPACE_RUN_TOKEN': 'fixture-capability'}}}},
        activity=SimpleNamespace(start=lambda *a: events.append(('start', a)),
            complete=lambda *a: events.append(('complete', a)), commentary=lambda text: None),
        step=lambda: None, cwd=str(workspace), definition=None, context_store=context_store)
    agent = create_agent()
    history = ([{'role': 'user', 'content': 'Continue the searchable dropdown task; do not repeat completed writes.'},
                {'role': 'assistant', 'tool_calls': [{'id': 'read1', 'type': 'function', 'function': {
                    'name': 'Read', 'arguments': '{"file_path":"build.log"}'}}]},
                {'role': 'tool', 'tool_call_id': 'read1', 'content': '\n'.join(
                    f'Build output {i}: completed operation with verbose diagnostic details.' for i in range(5000))}]
               if resumed else [])
    store = None
    if resumed == 'durable':
        store = ContextStore(session / 'context.sqlite3', 'transport-run')
        store.initialize([{'role': 'user', 'content': 'Keep Escape support. Do not deploy.'}, *history,
            *[{'role': 'assistant', 'content': 'Older completed step ' + str(i)} for i in range(80)]])
        def compact(previous, entries):
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
        assert calls == [{'name': 'echo', 'arguments': {'text': 'sdk-transport-ok'}}]
        expected = (['Read'] if resumed is True else []) + ['ToolSearch', 'mcp__moyai__echo']
        assert [args[1] for kind, args in events if kind == 'start'] == expected
        assert [kind for kind, _ in events] == ['start', 'complete'] * len(expected)
        first_tools = {tool['name'] for tool in requests[0]['tools']}
        assert {'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'ToolSearch'} <= first_tools
        assert not any(name.startswith('mcp__') for name in first_tools)
        assert not any('unused_' in tool['name'] for body in requests for tool in body['tools'])
        last_tools = {tool['name']: tool for tool in requests[-1]['tools']}
        assert last_tools['mcp__moyai__echo']['input_schema']['properties'] == {'text': {'type': 'string'}}
        assert last_tools['mcp__moyai__echo']['defer_loading'] is True
        assert 'tool_reference' in json.dumps(requests[-1]['messages'])
        assert not agent.journal.pending
        assert any(m.get('role') == 'tool' and 'sdk-transport-ok' in m['content'] for m in result['messages'])
        assert any('cache_control' in json.dumps(body) for body in requests)
        if resumed == 'durable':
            store.compact(relay.compact)
            cursor = store.state()['cursor']
            assert cursor > 0
            agent.close()
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
            assert not (session / '.moyai-history.jsonl').exists()
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
