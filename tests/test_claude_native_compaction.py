"""Exercise the bundled runtime's own compactor, with synthetic inference."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from agent.harnesses.claude_harness import ClaudeAgent
from agent.context_store import ContextStore


def send_message(handler, request, block, usage):
    handler.server.message_seq = getattr(handler.server, 'message_seq', 0) + 1
    message = {'id': f'msg_fixture_{handler.server.message_seq}', 'type': 'message', 'role': 'assistant',
        'model': request['model'], 'content': [block], 'stop_sequence': None,
        'stop_reason': 'tool_use' if block['type'] == 'tool_use' else 'end_turn',
        'usage': usage}
    if request.get('stream'):
        tool = block['type'] == 'tool_use'
        frames = [dict(type='message_start', message={**message, 'content': [], 'stop_reason': None}),
            dict(type='content_block_start', index=0, content_block={**block, 'input': {}} if tool else {'type': 'text', 'text': ''}),
            dict(type='content_block_delta', index=0, delta={'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                 if tool else {'type': 'text_delta', 'text': block['text']}),
            dict(type='content_block_stop', index=0),
            dict(type='message_delta', delta={'stop_reason': message['stop_reason'], 'stop_sequence': None}, usage={'output_tokens': usage['output_tokens']}),
            dict(type='message_stop')]
        raw = ''.join('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n' for frame in frames).encode()
        mime = 'text/event-stream'
    else:
        raw, mime = json.dumps(message).encode(), 'application/json'
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


@pytest.mark.parametrize('model', ['openai/gpt-6-astra', 'fireworks_ai/glm-5p3', 'anthropic/claude-opus-5-5'])
def test_native_compaction_repeats_without_replaying_tools(tmp_path, monkeypatch, model):
    requests, summaries, tools, comments = [], [], [], []
    for step in range(1, 17):
        (tmp_path / f'receipt-{step}.txt').write_text(f'Completed step {step}. Preserve codeword violet-pine.\n' + '\n'.join(f'Log item {i}: synthetic history to compact after reading.' for i in range(600)))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if '/count_tokens' in self.path:
                raw = json.dumps({'error': {'type': 'not_found', 'message': 'No counter on this relay'}}).encode()
                self.send_response(404)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            requests.append(body)
            summarizing = 'CRITICAL: Respond with TEXT ONLY.' in json.dumps(body)
            if summarizing:
                summaries.append(body)
                block = {'type': 'text', 'text': '<summary>native-summary-only-marker. Keep codeword violet-pine. Steps '
                    + ','.join(str(i) for i in range(1, len(tools) + 1)) + ' are completed. Do not repeat them.</summary>'}
            elif len(tools) < 16:
                step = len(tools) + 1
                block = {'type': 'tool_use', 'id': f'read_{step}', 'name': 'Read',
                         'input': {'file_path': str(tmp_path / f'receipt-{step}.txt')}}
            else:
                assert 'violet-pine' in json.dumps(body['messages'])
                block = {'type': 'text', 'text': 'native-compaction-ok violet-pine'}
            send_message(self, body, block, {'input_tokens': len(json.dumps(body['messages'])) // 4 + 2000,
                                           'output_tokens': 100})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'fixture')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'sdk-config'))
    # Both inherited flags must be overridden by the harness.
    monkeypatch.setenv('DISABLE_AUTO_COMPACT', '1')
    monkeypatch.setenv('DISABLE_COMPACT', '1')
    relay = SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}',
        context_window=lambda: {'input_budget': 70000},
        compact=lambda *a, **k: pytest.fail('Custom fallback must not replace native compaction'))
    store = ContextStore(tmp_path / 'journal.db', 'native-test')
    store.initialize([])
    agent = ClaudeAgent(spec={'model': model, 'timeout': 60, 'max_iterations': 40}, relay=relay,
        config={'mcp_servers': {'workspace': {}}}, activity=SimpleNamespace(
            start=lambda call_id, name, args: tools.append(call_id), complete=lambda *a: None,
            commentary=comments.append), step=lambda: None, cwd=str(tmp_path), definition=None, context_store=store)
    options = agent.options
    agent.options = lambda system: replace(options(system), tools=['Read'], allowed_tools=['Read'], mcp_servers={})
    try:
        result = agent.run_conversation('Read all sixteen receipt files in order, once each. Keep codeword violet-pine.',
            conversation_history=[], system_message='Read-only local verification.')
        assert result['completed'], result
        assert agent.native_compactions >= 2, (agent.native_compactions, len(summaries), comments)
        assert len(summaries) == agent.native_compactions
        assert tools == [f'read_{i}' for i in range(1, 17)]
        assert 'violet-pine' in result['final_response']
        assert len(requests) == 17 + len(summaries)
        assert not store.pending
        assert len([m for m in agent.journal.messages if m['role'] == 'tool']) == 16
        store.close()
        cold = ContextStore(tmp_path / 'journal.db', 'native-test')
        try:
            records = [json.loads(row[0]) for row in cold.db.execute('SELECT message FROM journal ORDER BY seq')]
            assert len([m for m in records if m['role'] == 'tool']) == 16
            assert 'native-summary-only-marker' not in json.dumps(records)
            assert cold.state()['summary'] == '' and not cold.pending
        finally:
            cold.close()
    finally:
        agent.close()
        store.close()
        server.shutdown()
        server.server_close()
