"""Pinned Codex SDK and real MCP, with synthetic inference/computer admission."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

from agent.context_store import ContextStore
from agent.harnesses.codex_harness import CodexAgent
from agent.tools.computer_tools import call as computer_call
from test_codex_sdk_transport import send_response


def test_warm_independent_sdk_keeps_process_but_refreshes_thread_and_tools(tmp_path, monkeypatch):
    from sandbox.codex_runtime import RuntimeLease
    from test_codex_tool_readiness import readiness_case
    proofs = []
    with tempfile.TemporaryDirectory(prefix='moyai-independent-', dir='/tmp') as folder:
        class IndependentAgent(CodexAgent):
            def __init__(self, **kwargs):
                kwargs['spec']['independent_runtime'] = True
                kwargs['config']['mcp_servers']['workspace']['env']['MOYAI_REMOTE_COMPUTER'] = '1'
                self.lease = RuntimeLease('same-private-scope', 5, root=Path(folder))
                kwargs['relay'].codex_runtime = self.lease
                super().__init__(**kwargs)

            def close(self):
                proof = {**(self.lease.info or {}), 'thread': self.runtime_thread}
                super().close()
                proof['clean'] = self.lease.clean
                self.lease.close()
                proofs.append(proof)

        first, _, result = readiness_case(tmp_path, monkeypatch, delay=0, agent_class=IndependentAgent)
        assert first['completed'], result
        second, requests, result = readiness_case(tmp_path, monkeypatch, delay=0,
            agent_class=IndependentAgent, capability='next-turn-capability', tool_name='fresh_lookup')
        assert second['completed'] and second['tool_calls'] == ['fresh_lookup'], result
        assert proofs[0]['clean'] and proofs[1]['clean']
        assert proofs[0]['pid'] == proofs[1]['pid'] and proofs[1]['reused']
        assert proofs[0]['thread'] != proofs[1]['thread']
        assert 'ready-before-inference' not in json.dumps(requests[0]['body']['input'])


def test_selected_sdk_uses_broker_before_later_computer_acquisition(tmp_path, monkeypatch):
    control, computer = tmp_path/'control', tmp_path/'computer'
    control.mkdir()
    requests, calls, operations, events, faults = [], [], {}, [], []
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'runtime-fixture')
    relay = SimpleNamespace()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            assert self.path == '/tools'
            self.reply([{'name': 'knowledge_lookup', 'description': 'Read the connected knowledge source.',
                         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.headers.get('Authorization') == 'Bearer runtime-fixture'
            if self.path == '/tools/call':
                assert body['name'] == 'knowledge_lookup' and not computer.exists()
                calls.append(body)
                return self.reply({'answer': 'useful-source-answer-before-computer'})
            if self.path == '/agent-runtime':
                assert calls and len(requests) == 2
                identity = body['request_id']
                if identity not in operations:
                    operations[identity] = {'request': body, 'status': 'queued'}
                    assert not computer.exists()
                    return self.reply({'status': 'queued', 'request_id': identity})
                assert operations[identity]['request'] == body
                computer.mkdir(exist_ok=True)
                result = computer_call(body['name'], body['arguments'], computer)
                operations[identity]['status'] = 'completed'
                return self.reply({'status': 'completed', 'request_id': identity,
                    'result': {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': False}})
            assert self.path == '/v1/responses'
            if not relay.before_model(json.dumps(body).encode()):
                faults.append('Unexpected blocked inference')
            requests.append(body)
            sequence = len(requests)
            if sequence == 1:
                assert not computer.exists()
                script = 'text(await tools.mcp__moyai__knowledge_lookup({}));'
            elif sequence == 2:
                assert 'useful-source-answer-before-computer' in json.dumps(body)
                assert not computer.exists()
                script = 'text(await tools.mcp__moyai__computer_write_file({path:"receipt.txt",text:"same-agent-result"}));'
            else:
                assert sequence == 3 and (computer/'receipt.txt').read_text() == 'same-agent-result'
                script = None
            output = ({'type': 'custom_tool_call', 'id': f'item_{sequence}', 'call_id': f'call_{sequence}',
                'name': 'exec', 'namespace': 'functions', 'input': script} if script else
                {'type': 'message', 'id': 'final', 'role': 'assistant', 'phase': 'final_answer',
                 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'independent-runtime-ok'}]})
            send_response(self, output, sequence)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay.url = f'http://127.0.0.1:{server.server_port}'
    store = ContextStore(tmp_path/'context.sqlite3', 'independent-runtime')
    store.initialize([])
    agent = CodexAgent(spec={'model': 'openai/gpt-6-astra', 'independent_runtime': True,
        'timeout': 45, 'max_iterations': 6}, relay=relay,
        config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1]/'agent/tools/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': 'runtime-fixture',
                    'MOYAI_REMOTE_COMPUTER': '1', 'MOYAI_WORKSPACE': str(computer)}}}},
        activity=SimpleNamespace(start=lambda *a: events.append(('start', a)),
            complete=lambda *a: events.append(('complete', a)), commentary=lambda *a: None),
        step=lambda: None, cwd=str(control), definition=None, context_store=store)
    try:
        result = agent.run_conversation('Read the source, then write the requested file.',
                                       conversation_history=[], system_message='Use the supplied broker and computer tools.')
        assert result['completed'] and result['final_response'] == 'independent-runtime-ok'
        assert not faults and len(requests) == 3 and len(calls) == len(operations) == 1
        assert {body['model'] for body in requests} == {'gpt-6-astra'}
        assert len({body['client_metadata']['thread_id'] for body in requests}) == 1
        assert not (control/'receipt.txt').exists()
        assert (computer/'receipt.txt').read_text() == 'same-agent-result'
        assert 'useful-source-answer-before-computer' in json.dumps(store.history())
    finally:
        agent.close()
        store.close()
        server.shutdown()
        server.server_close()
