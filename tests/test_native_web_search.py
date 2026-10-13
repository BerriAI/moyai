"""Native hosted search crosses the real SDK/broker wire; no external provider calls."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict
import pytest

from app.config import Settings
from app.deepagents_runtime import DeepAgentsRuntime
from app.deepagents_workflow import TurnScope
from app.model_selection import ASTRA, ASTRA_ULTRAFAST, gateway_model
from app.native_trace import NativeModelContent
from deepagents_provider import answer, response_stream
from sandbox.broker_transport import CONTENT_TYPE, unseal


SEARCH = {'id': 'ws_fixture', 'type': 'web_search_call', 'status': 'completed',
          'action': {'type': 'search', 'query': 'private-query-sentinel',
                     'sources': [{'type': 'url', 'url': 'https://example.com/release'}]}}
CITATION = {'type': 'url_citation', 'start_index': 0, 'end_index': 6,
            'title': 'Release', 'url': 'https://example.com/release'}


@pytest.mark.parametrize(('selected', 'active', 'allowlist', 'enabled'), [
    (ASTRA, '', None, True),
    (ASTRA, '', [], False),
    ('anthropic/claude-opus-5-5', '', None, False),
    ('custom-responses-alias', '', None, False),
    ('custom-responses-alias', '', ['custom-responses-alias'], True),
    (ASTRA_ULTRAFAST, '', None, False),
    (ASTRA_ULTRAFAST, '', [ASTRA_ULTRAFAST], True),
    (ASTRA, 'anthropic/claude-opus-5-5', None, False),
    ('anthropic/claude-opus-5-5', ASTRA, None, True),
])
async def test_native_search_model_policy_and_sdk_round_trip(selected, active, allowlist, enabled, monkeypatch):
    monkeypatch.delenv('NATIVE_WEB_SEARCH_MODELS', raising=False)
    configured = Settings(_env_file=None, **({} if allowlist is None else {'native_web_search_models': allowlist}))
    calls = []
    response = answer('Cited release.', 'native-search')
    response['model'] = gateway_model(active or selected)
    if enabled:
        response['output'].insert(0, copy.deepcopy(SEARCH))
        response['output'][-1]['content'][0]['annotations'] = [CITATION]

    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == '/broker/test/v1/responses'
            assert self.headers['Authorization'] == 'Bearer scoped-token'
            assert self.headers['Content-Type'] == CONTENT_TYPE
            raw = self.rfile.read(int(self.headers['Content-Length']))
            calls.append(json.loads(unseal('scoped-token', '/v1/responses', raw)))
            wire = b''.join(response_stream(response))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=edge.serve_forever, daemon=True)
    thread.start()
    configured.public_url = f'http://127.0.0.1:{edge.server_port}'
    runtime = object.__new__(DeepAgentsRuntime)
    runtime.runner = SimpleNamespace(settings=configured, token=lambda *_: 'scoped-token')
    runtime.current = lambda _: {'model': selected, 'active_model': active}
    scope = TurnScope('test', 1, model=gateway_model(active or selected))
    parameters = [{'type': 'function', 'function': {'name': 'workspace_tools', 'description': 'Discover tools.',
                   'parameters': {'type': 'object', 'properties': {}}}}]
    original = copy.deepcopy(parameters)
    try:
        result = await runtime._model_request(scope, [HumanMessage(content='Find a fresh release.')], {}, parameters)
        assert result.text == 'Cited release.'
        assert parameters == original
        assert len(calls) == 1
        payload = calls[0]
        assert payload['model'] == scope.model and payload['store'] is False
        assert 'previous_response_id' not in payload
        assert any(t.get('name') == 'workspace_tools' for t in payload['tools'])
        search_tools = [t for t in payload['tools'] if t['type'] == 'web_search']
        assert search_tools == ([{'type': 'web_search', 'search_context_size': 'low'}] if enabled else [])
        assert ('web_search_call.action.sources' in payload['include']) is enabled
        assert 'reasoning.encrypted_content' in payload['include']
        assert payload.get('tool_choice', 'auto') == 'auto'
        assert result.tool_calls == []  # Hosted search never becomes a workspace function.

        if enabled:
            restored = messages_from_dict(messages_to_dict([result]))[0]
            assert any(b.get('type') == 'web_search_call' and b['action'] == SEARCH['action']
                       for b in restored.content)
            assert any(CITATION in b.get('annotations', []) for b in restored.content)
            # Resending typed history must preserve the native receipt and citations.
            await runtime._model_request(scope, [HumanMessage(content='Find a release.'), restored,
                HumanMessage(content='Explain the cited release.')], {}, parameters)
            assert any(i.get('type') == 'web_search_call' and i['id'] == SEARCH['id']
                       for i in calls[1]['input'])
            assert CITATION in next(i for i in calls[1]['input']
                if i.get('role') == 'assistant')['content'][0]['annotations']
    finally:
        edge.shutdown()
        edge.server_close()
        thread.join()


def test_native_search_env_opt_out_and_custom_alias(monkeypatch):
    monkeypatch.setenv('NATIVE_WEB_SEARCH_MODELS', '[]')
    assert Settings(_env_file=None).native_web_search_models == []
    monkeypatch.setenv('NATIVE_WEB_SEARCH_MODELS', '["custom-responses-alias"]')
    assert Settings(_env_file=None).native_web_search_models == ['custom-responses-alias']


def test_native_search_trace_keeps_tool_identity_without_private_action_data():
    content = NativeModelContent({}, '/v1/responses')
    content.consume({'type': 'response.output_item.added', 'item': SEARCH})
    assert content.tools == ['web_search']
    content.consume({'type': 'response.completed', 'response': {'output': [SEARCH]}})
    assert content.choices == [{'message': {'content': '',
        'tool_calls': [{'function': {'name': 'web_search'}}]}}]
    assert 'private-query-sentinel' not in json.dumps(content.choices)
    assert 'example.com' not in json.dumps(content.choices)

    content.consume({'type': 'response.completed', 'response': {'output': [SEARCH] * 101}})
    assert len(content.tools) == 100


@pytest.mark.parametrize('status', ['failed', 'incomplete'])
def test_native_search_attempt_survives_unsuccessful_trace_snapshot(status):
    content = NativeModelContent({}, '/v1/responses')
    content.consume({'type': 'response.output_item.added', 'item': SEARCH})
    content.consume({'type': 'response.' + status, 'response': {'status': status, 'output': []}})
    assert content.tools == ['web_search']
