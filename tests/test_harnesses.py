import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.db import Store
from sandbox.broker_relay import BrokerRelay
from sandbox.broker_transport import unseal
from test_workspace import workspace
from test_slack import slack_app, event, signed
from test_slack_chat import send

OPUS = 'anthropic/claude-opus-5-5'


def test_session_harness_validation_persistence_and_idempotency(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    assert len(client.get('/api/config').json()['harnesses']) == 6
    assert client.post('/api/runs', json={'prompt': 'bad choice', 'harness': 'unknown'}).status_code == 422
    assert client.post('/api/runs', json={'prompt': 'bad model', 'harness': 'claude-agent-sdk', 'model': 'unconfigured/model'}).status_code == 422
    body = {'prompt': 'SDK task', 'harness': 'claude-agent-sdk', 'model': OPUS, 'client_id': 'harness-test-1'}
    response = client.post('/api/runs', json=body)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run['harness'] == 'claude-agent-sdk'
    assert client.post('/api/runs', json=body).json()['id'] == run['id']
    assert client.post('/api/runs', json={**body, 'harness': 'hermes'}).status_code == 409
    reopened = Store(app.state.settings.data_dir)
    assert reopened.run(run['id'])['harness'] == 'claude-agent-sdk'
    endpoint = f"/api/runs/{run['id']}/messages"
    assert client.post(endpoint, json={'content': 'follow up', 'client_id': 'harness-followup-astra', 'model': 'astra'}).status_code == 202
    assert client.post(endpoint, json={'content': 'follow up', 'client_id': 'harness-followup'}).status_code == 202
    assert app.state.manager.spec(app.state.store.run(run['id']))['harness'] == 'claude-agent-sdk'
    legacy = client.post('/api/runs', json={'prompt': 'default task'}).json()
    assert legacy['harness'] == 'hermes'


def test_slack_harness_command_and_followups(slack_app):
    app, client, submitted, _ = slack_app
    body = event(text='<@U99999999> harness claude-agent-sdk')
    assert client.post('/hooks/slack/events', **signed(body)).status_code == 200
    assert not submitted
    run = app.state.store.rows('SELECT * FROM runs')[0]
    assert run['harness'] == 'claude-agent-sdk' and run['model'] == app.state.settings.resolve_model()
    send(client, 1, '<@U99999999> Read the repository')
    assert submitted[-1]['harness'] == 'claude-agent-sdk'
    send(client, 2, '<@U99999999> harness hermes')
    assert app.state.store.run(run['id'])['harness'] == 'claude-agent-sdk'
    assert 'fixed' in app.state.store.rows("SELECT text FROM slack_outbox WHERE dedupe_key='command:EvChat2'")[0]['text']
    send(client, 3, '<@U99999999> model astra')
    assert app.state.store.run(run['id'])['model'] == 'openai/gpt-6-astra'


@pytest.mark.parametrize('harness', ['hermes', 'claude-agent-sdk', 'codex', 'opencode', 'deepagents', 'tool-loop'])
def test_slack_harness_with_initial_task(slack_app, harness):
    app, client, submitted, _ = slack_app
    app.state.settings.agent_model = 'custom-provider-alias'
    body = event(text=f'<@U99999999> harness {harness}\nRead the repository')
    assert client.post('/hooks/slack/events', **signed(body)).status_code == 200
    assert len(submitted) == 1
    assert submitted[0]['harness'] == harness
    assert submitted[0]['model'] == 'custom-provider-alias'
    assert app.state.store.messages(submitted[0]['id'])[0]['content'] == 'Read the repository'


def test_journal_retains_completed_tool_receipts():
    from sandbox.harness_agent import TurnJournal
    journal = TurnJournal([], 'request')
    journal.tool_started('one', 'Write', {'path': 'proof.py'})
    assert journal.pending == {'one'}
    journal.tool_finished('one', 'written')
    assert not journal.pending
    assert journal.messages[-1]['tool_call_id'] == 'one'


def test_sdk_wire_uses_existing_sealed_model_broker():
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            assert self.path == '/broker/run/v1/messages'
            value = json.loads(unseal('test-token', '/v1/messages', self.rfile.read(int(self.headers['Content-Length']))))
            calls.append(value)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{edge.server_port}/broker/run', 'test-token').start()
    try:
        with httpx.Client(base_url=relay.url) as client:
            payload = {'messages': [{'role': 'user', 'content': 'test'}], 'stream': True}
            assert client.post('/v1/messages', json=payload).status_code == 401
            response = client.post('/v1/messages?beta=true', headers={'x-api-key': 'test-token'}, json=payload)
            assert response.status_code == 200 and 'message_stop' in response.text
            assert calls[0] == payload
            relay.before_model = lambda: False
            assert client.post('/v1/messages', headers={'x-api-key': 'test-token'}, json=payload).status_code == 409
            assert len(calls) == 1
    finally:
        relay.close()
        edge.shutdown()
        edge.server_close()


def test_claude_boundary_preserves_receipts_without_recursive_history():
    from types import SimpleNamespace
    from sandbox.litellm_harness import LiteLLMAgent
    from sandbox.harness_registry import resolve
    from sandbox.continuation import RotationDeadline
    relay = SimpleNamespace(before_model=None)
    agent = LiteLLMAgent(spec={}, relay=relay, config={}, activity=None, step=lambda: None, cwd='/workspace', definition=resolve('claude-agent-sdk'))
    from sandbox.harness_agent import TurnJournal, HarnessContext
    agent.journal = TurnJournal([{'role': 'user', 'content': 'previous'}, {'role': 'assistant', 'content': 'done'}], 'followup')
    agent.context = HarnessContext({}, relay, {}, None, agent.interrupt, '/workspace')
    agent.journal.tool_started('write1', 'Write', {})
    assert agent.before_model() is True
    agent.journal.tool_finished('write1', 'write completed')
    assert agent.before_model() is False
    assert agent.journal.messages[2]['content'] == 'followup'
    assert len(agent.journal.messages) == 5
    deadline = RotationDeadline(0)
    deadline.requested = True
    assert deadline.can_continue({'interrupted': True, 'messages': agent.journal.messages})
    agent.close()
    assert relay.before_model is None


def test_claude_private_tool_prefixes_are_scrubbed():
    from sandbox.memory_history import scrub_memory_history
    from sandbox.activity import ActivityReporter
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'm', 'function': {
        'name': 'mcp__moyai__memory_save', 'arguments': '{"content":"private note"}'}}]}]
    assert 'private note' not in json.dumps(scrub_memory_history(messages))
    events = []
    reporter = ActivityReporter(lambda *args: events.append(args), tracing=True)
    reporter.start('m', 'mcp__moyai__memory_save', {'content': 'private note'})
    reporter.complete('m', 'mcp__moyai__memory_save', {'content': 'private note'}, {})
    assert 'private note' not in json.dumps(events)


def test_native_images_are_not_translated():
    from app.harness_gateway import authorized_payload
    messages = [{'role': 'user', 'content': [{'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}]}]
    result = authorized_payload({'messages': messages}, '/v1/messages', OPUS, '')
    assert result['messages'] == messages


def test_agent_entrypoint_dispatches_claude_without_importing_hermes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sandbox import agent, claude_harness
    events = []
    relay = SimpleNamespace(url='http://test', control=lambda body=None: {}, last_error='',
                            wait_group='', wait_credential='', before_model=None, compact=lambda *a: 'Summary')
    class FakeClaude:
        def __init__(self, **kwargs):
            assert kwargs['spec']['harness'] == 'claude-agent-sdk'
            self.context_store = kwargs['context_store']
        def run_conversation(self, prompt, **kwargs):
            from sandbox.harness_agent import TurnJournal
            journal = TurnJournal([], prompt, self.context_store)
            journal.finish('SDK result')
            return {'completed': True, 'final_response': 'SDK result',
                    'messages': journal.messages}
        def close(self): pass
        def validate(self): pass
    monkeypatch.setattr(claude_harness, 'ClaudeAgent', FakeClaude)
    monkeypatch.setattr(agent, 'prepare_attachments', lambda *a, **k: None)
    monkeypatch.setattr(agent, 'apply_hermes_patches', lambda: pytest.fail('Claude must not patch Hermes'))
    monkeypatch.setattr(agent, 'prepare_project', lambda *a, **k: None)
    monkeypatch.setattr(agent, 'collect_archive', lambda *a: None)
    monkeypatch.setattr(agent, 'computer_request', lambda *a, **k: {})
    monkeypatch.setattr(agent, 'Path', lambda value: tmp_path / str(value).lstrip('/'))
    monkeypatch.setattr(agent, 'emit', lambda kind, message, data=None, **extra: events.append((kind, message, extra)))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'test-capability')
    monkeypatch.setenv('HERMES_HOME', '/home')
    monkeypatch.chdir(tmp_path)
    spec = {'run_id': 'harness-test', 'harness': 'claude-agent-sdk', 'broker_url': 'http://test', 'repo_url': '', 'model': OPUS,
            'prompt': 'test request', 'chat_enabled': True, 'max_iterations': 2, 'timeout': None}
    assert agent.run_agent(spec, relay) == 0
    assert next(e for e in events if e[0] == 'final')[1:] == ('SDK result', {
        'completed': True, 'continuation': False, 'wait_group': '', 'wait_credential': '',
        'steer_message_id': None, 'steering_applied': []})
    from sandbox.context_store import read_records
    assert json.loads(read_records(tmp_path / 'session/context.sqlite3')[-1]['text'])['content'] == 'SDK result'


def test_registry_extension_reaches_api_without_changing_entrypoint(workspace, monkeypatch):
    from sandbox.harness_registry import HARNESSES, HarnessDefinition, create_agent
    from sandbox import litellm_harness
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    definition = HarnessDefinition('test-adapter', 'Test adapter', 'litellm_harness', 'LiteLLMAgent',
                                   litellm_harness='CODEX', runtime_binding='test')
    monkeypatch.setitem(HARNESSES, definition.id, definition)
    assert definition.public() in client.get('/api/config').json()['harnesses']
    run = client.post('/api/runs', json={'prompt': 'registry test', 'harness': definition.id}).json()
    assert run['harness'] == definition.id
    assert client.post('/api/runs', json={'prompt': 'other provider', 'harness': definition.id, 'model': OPUS}).status_code == 201
    marker = object()
    monkeypatch.setattr(litellm_harness, 'LiteLLMAgent', lambda **context: (context['definition'], marker))
    assert create_agent(definition.id) == (definition, marker)


def test_unregistered_litellm_binding_fails_before_startup():
    from types import SimpleNamespace
    from sandbox.harness_registry import HarnessDefinition
    from sandbox.litellm_harness import LiteLLMAgent
    definition = HarnessDefinition('unsupported', 'Unsupported', 'litellm_harness', 'LiteLLMAgent', litellm_harness='CODEX')
    agent = LiteLLMAgent(spec={}, relay=SimpleNamespace(), config={}, activity=None, step=lambda: None,
                        cwd='/workspace', definition=definition)
    with pytest.raises(ValueError, match='No verified'):
        agent.validate()


@pytest.mark.parametrize('harness', ['hermes', 'claude-agent-sdk', 'codex', 'opencode', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('model', [OPUS, 'openai/gpt-6-astra', 'fireworks_ai/glm-5p3', 'custom-alias'])
def test_all_litellm_harnesses_can_be_selected(workspace, monkeypatch, harness, model):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    app.state.settings.agent_model = 'custom-alias'
    result = client.post('/api/runs', json={'prompt': 'run this harness', 'harness': harness, 'model': model})
    assert result.status_code == 201, result.text
    assert result.json()['harness'] == harness
    assert result.json()['model'] == model
    assert client.post('/api/runs', json={'prompt': 'reject unconfigured model', 'harness': harness, 'model': 'not-enabled'}).status_code == 422
    assert app.state.manager.spec(app.state.store.run(result.json()['id']))['harness'] == harness


def test_catalog_covers_upstream_harness_enum():
    litellm = pytest.importorskip('litellm.harness')
    from sandbox.harness_registry import HARNESSES
    assert {h.litellm_harness for h in HARNESSES.values() if h.litellm_harness} | {'CLAUDE_CODE'} == {h.name for h in litellm.Harness}


def test_responses_input_is_not_translated():
    from app.harness_gateway import authorized_payload
    from fastapi import HTTPException
    items = [{'type': 'function_call_output', 'call_id': 'one', 'output': 'saved receipt'}]
    result = authorized_payload({'input': items, 'api_key': 'untrusted'}, '/v1/responses', 'openai/test', 'context')
    assert result['input'] == items
    assert result['instructions'] == 'context\n\n'
    assert 'api_key' not in result
    with pytest.raises(HTTPException):
        authorized_payload({'input': [], 'previous_response_id': 'foreign'}, '/v1/responses', 'openai/test', '')


@pytest.mark.parametrize('name', ['moyai_memory_save', 'mcp__moyai__memory_save', 'workspace_call'])
def test_all_harnesses_scrub_private_tool_aliases(name):
    from sandbox.memory_history import scrub_memory_history
    from sandbox.activity import ActivityReporter
    message = {'role': 'assistant', 'tool_calls': [{'function': {'name': name, 'arguments': 'private note'}}]}
    assert 'private note' not in json.dumps(scrub_memory_history([message]))
    events = []
    reporter = ActivityReporter(lambda *args: events.append(args), tracing=True)
    reporter.start('one', name, {'content': 'private note'})
    reporter.complete('one', name, {'content': 'private note'}, 'private note')
    assert 'private note' not in json.dumps(events)
