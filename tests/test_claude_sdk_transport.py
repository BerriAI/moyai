"""Real bundled SDK process and real MCP transport; inference is a local fixture.

No provider calls or generated shell commands. The sole tool echoes test data.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from sandbox.claude_harness import ClaudeAgent


def test_real_sdk_executes_mcp_and_preserves_receipt(tmp_path, monkeypatch):
    calls, requests, events = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            assert self.path == '/tools'
            self.reply([{'name': 'echo', 'description': 'Echo synthetic test data',
                         'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}])
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/tools/call':
                calls.append(data)
                return self.reply({'text': data['arguments']['text']})
            assert self.path.startswith('/v1/messages')
            requests.append(data)
            done = bool(calls)
            block = ({'type': 'text', 'text': 'sdk-transport-ok'} if done else
                     {'type': 'tool_use', 'id': 'tool_echo', 'name': 'mcp__moyai__echo', 'input': {'text': 'sdk-transport-ok'}})
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
    agent = ClaudeAgent(spec={'model': 'anthropic/claude-sonnet-4-5', 'timeout': 30, 'max_iterations': 3},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': url, 'WORKSPACE_RUN_TOKEN': 'fixture-capability'}}}},
        activity=SimpleNamespace(start=lambda *a: events.append(('start', a)),
            complete=lambda *a: events.append(('complete', a)), commentary=lambda text: None),
        step=lambda: None, cwd=str(tmp_path), definition=None)
    try:
        result = agent.run_conversation('Call the echo tool.', conversation_history=[], system_message='Transport fixture.')
        assert result['completed'], result['final_response']
        assert result['final_response'] == 'sdk-transport-ok'
        assert calls == [{'name': 'echo', 'arguments': {'text': 'sdk-transport-ok'}}]
        assert [kind for kind, _ in events] == ['start', 'complete']
        assert not agent.journal.pending
        assert any(m.get('role') == 'tool' and 'sdk-transport-ok' in m['content'] for m in result['messages'])
        assert any('cache_control' in json.dumps(body) for body in requests)
    finally:
        agent.close()
        server.shutdown()
        server.server_close()
