"""Real pinned Codex + Moyai MCP; inference and the remote broker are fixtures."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent.harnesses import codex_harness
from agent.harnesses.codex_harness import CodexAgent
from test_codex_sdk_transport import send_response


def readiness_case(tmp_path, monkeypatch, *, delay=2, fail_tools=False, progress=lambda value: None,
                   agent_class=CodexAgent, capability='readiness-fixture', tool_name='echo'):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', capability)
    requests, calls, events = [], [], []
    catalog_ready = threading.Event()
    relay = SimpleNamespace()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except BrokenPipeError:
                pass  # The timeout case closes MCP while discovery is pending.

        def do_GET(self):
            assert self.path == '/tools'
            assert self.headers['Authorization'] == 'Bearer ' + capability
            progress('MCP is loading the workspace catalog...')
            time.sleep(delay)
            if fail_tools:
                self.send_error(503)
                return
            catalog_ready.set()
            self.reply([{'name': tool_name, 'description': 'Echo a verification marker.',
                'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                'required': ['text'], 'additionalProperties': False}}])
            progress('Workspace catalog loaded.')

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(raw)
            assert self.headers['Authorization'] == 'Bearer ' + capability
            if self.path == '/tools/call':
                assert body['name'] == tool_name
                calls.append(body['name'])
                progress('Real MCP tool executed: ' + body['name'])
                return self.reply({'text': body['arguments']['text']})
            assert self.path == '/v1/responses'
            assert relay.before_model(raw)
            requests.append({'ready': catalog_ready.is_set(), 'body': body})
            progress(f'Model request {len(requests)}: catalog ready = {catalog_ready.is_set()}')
            if len(requests) == 1:
                output = {'type': 'custom_tool_call', 'id': 'item_1', 'call_id': 'call_1',
                    'name': 'exec', 'namespace': 'functions',
                    'input': 'text(await tools.mcp__moyai__' + tool_name + '({text: "ready-before-inference"}));'}
            else:
                output = {'type': 'message', 'id': 'msg_2', 'role': 'assistant',
                    'phase': 'final_answer', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Tools were ready before the first request.'}]}
            send_response(self, output, len(requests))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False  # Settle the delayed read before the next case.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay.url = f'http://127.0.0.1:{server.server_port}'
    agent = agent_class(spec={'model': 'openai/gpt-6-astra', 'timeout': 30, 'max_iterations': 3},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'agent/tools/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': capability}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
            complete=lambda *args: events.append(('complete', args)),
            commentary=lambda text: events.append(('commentary', text))),
        step=lambda: None, cwd=str(tmp_path), definition=None)
    try:
        result = agent.run_conversation('Use the workspace echo tool.',
                                        conversation_history=[], system_message='Moyai tool test.')
        proof = {'model_started_after_catalog': bool(requests and requests[0]['ready']),
                 'tool_calls': calls, 'completed': result['completed']}
        progress(json.dumps(proof))
        return proof, requests, result
    finally:
        agent.close()
        server.shutdown()
        server.server_close()


def test_first_model_request_waits_for_actual_mcp_catalog(tmp_path, monkeypatch):
    proof, requests, result = readiness_case(tmp_path, monkeypatch, progress=print)
    assert proof == {'model_started_after_catalog': True, 'tool_calls': ['echo'], 'completed': True}
    assert 'ready-before-inference' in json.dumps(requests[-1]['body'])


@pytest.mark.parametrize('failure', ['unavailable', 'timeout'])
def test_failed_mcp_startup_does_not_spend_inference_or_claim_completion(tmp_path, monkeypatch, failure):
    if failure == 'timeout':
        monkeypatch.setattr(codex_harness, 'MCP_STARTUP_TIMEOUT_SECONDS', 0.2)
    proof, requests, result = readiness_case(tmp_path, monkeypatch, delay=1,
                                            fail_tools=failure == 'unavailable')
    assert not requests and not proof['tool_calls']
    assert not result['completed'] and result['failed']
    assert result['sdk_failure']['model_calls'] == 0
