import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.config import MODEL_CATALOG
from app.db import Store
from sandbox.broker_relay import BrokerRelay, InputPending
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



def use_model_defaults(settings):
    # The shared fixtures explicitly opt into Hermes for historical assertions.
    settings.agent_harness = 'claude-agent-sdk'
    settings.model_fields_set.discard('agent_harness')


@pytest.mark.parametrize(('model', 'harness'), [('astra', 'codex'), ('sol', 'codex'), ('opus', 'claude-agent-sdk'), ('glm', 'claude-agent-sdk')])
def test_web_new_session_pairs_model_once_and_exposes_effective_defaults(workspace, monkeypatch, model, harness):
    app, client = workspace
    use_model_defaults(app.state.settings)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    catalog = client.get('/api/config').json()
    assert catalog['harness'] == 'codex'
    canonical = app.state.settings.resolve_model(model)
    assert next(item for item in catalog['models'] if item['id'] == canonical)['default_harness'] == harness
    response = client.post('/api/runs', json={'prompt': 'Use the model default', 'model': model})
    assert response.status_code == 201, response.text
    run = response.json()
    assert (run['model'], run['harness']) == (canonical, harness)
    switched = client.post(f"/api/runs/{run['id']}/messages", json={'content': 'Now use the other model', 'client_id': 'switch-model-pairing', 'model': 'opus' if model == 'astra' else 'astra'})
    assert switched.status_code == 202, switched.text
    assert app.state.store.run(run['id'])['harness'] == harness
    child = client.post('/api/runs', json={'prompt': 'Continue in a side chat', 'side_chat_of': run['id'], 'model': 'astra'})
    assert child.status_code == 201, child.text
    assert child.json()['harness'] == harness
    explicit = client.post('/api/runs', json={'prompt': 'Keep my selected harness', 'model': model, 'harness': 'hermes'})
    assert explicit.json()['harness'] == 'hermes'


@pytest.mark.parametrize(('model', 'harness'), [
    ('openai/future-model', 'codex'),
    ('anthropic/future-model', 'claude-agent-sdk'),
    ('other/future-model', 'claude-agent-sdk'),
])
def test_new_catalog_models_inherit_provider_default(workspace, monkeypatch, model, harness):
    app, client = workspace
    use_model_defaults(app.state.settings)
    monkeypatch.setitem(MODEL_CATALOG, model, 'Future Model')
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    catalog = client.get('/api/config').json()
    assert {'id': model, 'name': 'Future Model', 'default_harness': harness} in catalog['models']
    # A new display-name alias also routes after resolving to its canonical ID.
    response = client.post('/api/runs', json={'prompt': 'Use the new model', 'model': 'Future Model'})
    assert response.status_code == 201, response.text
    run = response.json()
    assert (run['model'], run['harness']) == (model, harness)
    assert app.state.manager.spec(app.state.store.run(run['id']))['harness'] == harness


@pytest.mark.parametrize('thread_chat', [True, False])
@pytest.mark.parametrize(('model', 'harness'), [('openai/gpt-6-astra', 'codex'), ('openai/gpt-6.1-sol', 'codex'), (OPUS, 'claude-agent-sdk')])
def test_slack_new_sessions_pair_configured_model(slack_app, thread_chat, model, harness):
    app, client, submitted, _ = slack_app
    use_model_defaults(app.state.settings)
    app.state.settings.agent_model = model
    app.state.settings.slack_thread_chat_enabled = thread_chat
    response = client.post('/hooks/slack/events', **signed(event()))
    assert response.status_code == 200, response.text
    assert (submitted[0]['model'], submitted[0]['harness']) == (model, harness)


@pytest.mark.parametrize('model', ['astra', 'sol'])
def test_slack_selected_model_pairs_only_new_thread(slack_app, model):
    app, client, submitted, _ = slack_app
    use_model_defaults(app.state.settings)
    app.state.settings.agent_model = OPUS
    response = client.post('/hooks/slack/events', **signed(event(text=f'<@U99999999> model {model}')))
    assert response.status_code == 200, response.text
    run = app.state.store.rows('SELECT * FROM runs')[0]
    assert (run['model'], run['harness']) == (app.state.settings.resolve_model(model), 'codex')
    send(client, 1, '<@U99999999> model opus')
    current = app.state.store.run(run['id'])
    assert (current['model'], current['harness']) == (OPUS, 'codex')


@pytest.mark.parametrize(('model', 'harness'), [('astra', 'codex'), ('sol', 'codex'), ('opus', 'claude-agent-sdk')])
def test_new_automation_default_persists_across_edits_and_launches(workspace, monkeypatch, model, harness):
    app, client = workspace
    use_model_defaults(app.state.settings)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    definition = {'name': 'Model workflow', 'prompt': 'Read the project', 'mode': 'demo', 'model': model}
    created = client.post('/api/automations', json={'definition': definition})
    assert created.status_code == 201, created.text
    automation = created.json()
    assert automation['definition']['harness'] == harness
    endpoint = f"/api/automations/{automation['id']}"
    edited = client.put(endpoint, json={'revision': 1, 'definition': {**definition, 'model': 'opus' if model == 'astra' else 'astra'}})
    assert edited.status_code == 200, edited.text
    assert edited.json()['definition']['harness'] == harness
    launched = client.post(endpoint + '/run', json={'revision': 2, 'client_id': 'paired-harness-launch'})
    assert launched.status_code == 202, launched.text
    assert app.state.store.run(launched.json()['run_id'])['harness'] == harness
    explicit = client.post('/api/automations', json={'definition': {**definition, 'harness': 'hermes'}})
    assert explicit.json()['definition']['harness'] == 'hermes'
    # Saved definitions that predate harness selection keep their legacy engine.
    stored = json.loads(app.state.automations.row(automation['id'])['definition'])
    stored.pop('harness')
    app.state.store.execute('UPDATE automations SET definition=? WHERE id=?', (json.dumps(stored), automation['id']))
    legacy = client.put(endpoint, json={'revision': 2, 'definition': definition})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()['definition']['harness'] == 'hermes'

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
            relay.before_model = lambda request: False
            assert client.post('/v1/messages', headers={'x-api-key': 'test-token'}, json=payload).status_code == 409
            def input_boundary(request):
                raise InputPending()
            relay.before_model = input_boundary
            response = client.post('/v1/messages', headers={'x-api-key': 'test-token'}, json=payload)
            assert response.status_code == 400 and response.json()['error']['code'] == 'moyai_input_pending'
            assert not relay.model_failed and not relay.last_error and relay.last_failure is None
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


@pytest.mark.parametrize('second_stops', [False, True])
def test_overlapping_model_requests_keep_their_own_boundary_outcomes(tmp_path, second_stops):
    from concurrent.futures import ThreadPoolExecutor
    from sandbox.harness_agent import HarnessInputs, TurnJournal
    from sandbox.harness_registry import create_agent
    first_fenced, second_entered, release_second = (threading.Event() for _ in range(3))
    steps = []
    def step():
        steps.append(True)
        if len(steps) == 2:
            if second_stops:
                agent.interrupt()
            second_entered.set()
            assert release_second.wait(5)
    relay = BrokerRelay('http://127.0.0.1:1', 'test-token').start()
    agent = create_agent('tool-loop', spec={}, relay=relay, config={}, activity=None, step=step, cwd=str(tmp_path))
    agent.journal = TurnJournal([], 'task')
    agent.inputs = HarnessInputs(agent.journal)
    assert agent.accept_input({'id': 1, 'content': 'correction'})
    def boundary(raw):
        try:
            return agent.before_model(raw)
        except InputPending:
            if json.loads(raw)['request'] == 'first':
                first_fenced.set()
                assert second_entered.wait(5)
            raise
    relay.before_model = boundary
    def request(name):
        return httpx.post(relay.url + '/v1/chat/completions', json={'request': name},
            headers={'Authorization': 'Bearer test-token'}, timeout=10)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(request, 'first')
            assert first_fenced.wait(5)
            second = executor.submit(request, 'second')
            response = first.result(timeout=5)
            release_second.set()
            other = second.result(timeout=5)
        assert response.status_code == 400 and response.json()['error']['code'] == 'moyai_input_pending'
        assert other.status_code == (409 if second_stops else 400)
        assert agent.model_calls == 0 and not relay.last_failure and not relay.model_failed
    finally:
        release_second.set()
        agent.close()
        relay.close()


def test_claude_private_tool_prefixes_are_scrubbed():
    from sandbox.memory_history import scrub_memory_history
    from sandbox.activity import ActivityReporter
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'm', 'function': {
        'name': 'mcp__moyai__memory_save', 'arguments': '{"content":"private note"}'}}]}]
    assert 'private note' not in json.dumps(scrub_memory_history(messages))
    events = []
    reporter = ActivityReporter(lambda *args: events.append(args), tracing=True, omit_private_tool_payloads=True)
    reporter.start('m', 'mcp__moyai__memory_save', {'content': 'private note'})
    reporter.complete('m', 'mcp__moyai__memory_save', {'content': 'private note'}, {})
    assert 'private note' not in json.dumps(events)


def test_native_images_are_not_translated():
    from app.harness_gateway import authorized_payload
    messages = [{'role': 'user', 'content': [{'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': 'fixture'}}]}]
    result = authorized_payload({'messages': messages}, '/v1/messages', OPUS, '')
    assert result['messages'] == messages


@pytest.mark.parametrize('transport_failure', [False, True])
def test_agent_entrypoint_dispatches_claude_without_importing_hermes(tmp_path, monkeypatch, transport_failure):
    from types import SimpleNamespace
    from threading import Event
    from sandbox import agent, claude_harness
    events = []
    relay = SimpleNamespace(url='http://test', control=lambda body=None: {}, last_error='',
                            wait_group='', wait_credential='', before_model=None, compact=lambda *a: 'Summary')
    class FakeClaude:
        def __init__(self, **kwargs):
            assert kwargs['spec']['harness'] == 'claude-agent-sdk'
            self.context_store = kwargs['context_store']
            self.context = SimpleNamespace(relay=relay)
            self.stopped = Event()
            self.transport_attempt = 2
        def run_conversation(self, prompt, **kwargs):
            from sandbox.harness_agent import TurnJournal
            journal = self.journal = TurnJournal([], prompt, self.context_store)
            if transport_failure:
                journal.tool_started('write-once', 'Write', {})
                journal.tool_finished('write-once', 'Saved receipt')
                relay.last_error = 'HTTP 502'
                relay.last_failure = {'version': 1, 'route': '/v1/messages', 'http_status': 502,
                                      'transient': True, 'response_started': False, 'request_id': 'failure-fixture'}
                return {'failed': True, 'messages': journal.messages}
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
    assert agent.run_agent(spec, relay) == (75 if transport_failure else 0)
    assert next(e[2] for e in events if e[0] == 'final')['transport_attempt'] == 2
    if transport_failure:
        final = next(e[2] for e in events if e[0] == 'final')
        assert not final['completed'] and not final['continuation']
        assert final['transport_retry']['failure']['request_id'] == 'failure-fixture'
        from sandbox.context_store import ContextStore
        saved = ContextStore(tmp_path / 'session/context.sqlite3', 'harness-test')
        assert saved.checkpoint() == final['transport_retry']['checkpoint']
        assert 'Saved receipt' in saved.history()[0]['content'] and not saved.pending
        saved.close()
        return
    assert next(e for e in events if e[0] == 'final')[1:] == ('SDK result', {
        'completed': True, 'continuation': False, 'wait_group': '', 'wait_credential': '',
        'steer_message_id': None, 'steering_applied': [], 'transport_attempt': 2})
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


@pytest.fixture
def installed_litellm_runtime(monkeypatch):
    import sys
    from pathlib import Path
    from types import ModuleType, SimpleNamespace
    from sandbox import harness_dependencies
    litellm = ModuleType('litellm')
    litellm.Harness, litellm.aagent_session = object(), object()
    monkeypatch.setitem(sys.modules, 'litellm', litellm)
    for name in ('claude_agent_sdk', 'deepagents', 'langchain_litellm'):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    source = harness_dependencies.LITELLM_SOURCE / 'litellm' / 'harness'
    is_dir = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda path: False if path == source else is_dir(path))
    monkeypatch.setattr(harness_dependencies.shutil, 'which', lambda name: '/prepared/bin/' + name)
    def version_only(command, **kwargs):
        assert command == ['opencode', '--version'], 'Unexpected runtime install'
        return SimpleNamespace(returncode=0, stdout='1.18.35\n')
    monkeypatch.setattr(harness_dependencies.subprocess, 'run', version_only)
    return harness_dependencies


@pytest.mark.parametrize('harness', ['opencode', 'deepagents', 'tool-loop'])
def test_ready_litellm_harness_does_not_require_codex(installed_litellm_runtime, monkeypatch, harness):
    from types import SimpleNamespace
    from sandbox.harness_registry import create_agent
    def unavailable_codex():
        raise RuntimeError('Codex is unavailable')
    monkeypatch.setattr(installed_litellm_runtime, 'prepare_codex', unavailable_codex)
    agent = create_agent(harness, spec={}, relay=SimpleNamespace(), config={}, activity=None,
                         step=lambda: None, cwd='/workspace')
    try:
        agent.validate()
    finally:
        agent.close()


@pytest.mark.parametrize('entrypoint', ['codex', 'image'])
@pytest.mark.parametrize('install_fails', [False, True])
def test_native_validation_and_image_entrypoint_require_codex(installed_litellm_runtime, monkeypatch, entrypoint, install_fails):
    import importlib.metadata
    import runpy
    import subprocess
    from types import SimpleNamespace
    from sandbox.harness_registry import create_agent
    dependencies, calls = installed_litellm_runtime, []
    installed_version = importlib.metadata.version
    def version(package):
        if package == 'openai-codex':
            raise importlib.metadata.PackageNotFoundError(package)
        return installed_version(package)
    def install(args, **kwargs):
        calls.append(args)
        if args[1:4] == ['-m', 'pip', 'install'] and install_fails:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(importlib.metadata, 'version', version)
    monkeypatch.setattr(dependencies.subprocess, 'run', install)
    agent = (create_agent('codex', spec={}, relay=SimpleNamespace(), config={}, activity=None,
                          step=lambda: None, cwd='/workspace') if entrypoint == 'codex' else None)
    def invoke():
        if agent is not None:
            agent.validate()
        else:
            runpy.run_path(dependencies.__file__, run_name='__main__')
    try:
        if install_fails:
            with pytest.raises(subprocess.CalledProcessError):
                invoke()
        else:
            invoke()
        installs = [args[1:] for args in calls if args[1:4] == ['-m', 'pip', 'install']]
        assert installs == [['-m', 'pip', 'install', 'openai-codex==0.161.0']]
    finally:
        if agent is not None:
            agent.close()


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
    assert {h.litellm_harness for h in HARNESSES.values() if h.litellm_harness} | {'CLAUDE_CODE', 'CODEX'} == {h.name for h in litellm.Harness}


def test_native_compaction_controls_keep_other_runtime_settings():
    pytest.importorskip('litellm.harness')
    from sandbox.harness_bindings import opencode_options, tool_loop_options
    server = {'command': '/prepared/python', 'args': ['bridge.py'], 'env': {'FIXTURE': '1'}}
    config = {'mcp_servers': {'workspace': server}}
    assert 'compaction' not in opencode_options(config).config
    options = opencode_options({**config, 'live_compaction': True})
    assert options.config['compaction'] == {'auto': False, 'prune': False}
    assert options.config['mcp']['moyai'] == {'type': 'local',
        'command': ['/prepared/python', 'bridge.py'], 'environment': {'FIXTURE': '1'}, 'enabled': True}
    assert tool_loop_options({}).completion_kwargs == {'num_retries': 0}


def test_real_deepagents_disables_blocking_summary_in_main_and_children():
    pytest.importorskip('litellm.harness')
    pytest.importorskip('deepagents')
    pytest.importorskip('langchain_litellm')
    import subprocess
    import sys
    # The supported profile registration is process-scoped, like each sandbox
    # runtime. Use a fresh process to verify both capability choices honestly.
    result = subprocess.run([sys.executable, '-c', r"""
from langchain_litellm import ChatLiteLLM
import deepagents
import deepagents.graph
import deepagents.middleware.subagents
from sandbox.harness_bindings import deepagents_options
middleware = []
def capture(original):
    def build(*args, **kwargs):
        middleware.append({item.name for item in kwargs['middleware']})
        return original(*args, **kwargs)
    return build
for module in (deepagents.graph, deepagents.middleware.subagents):
    module.create_agent = capture(module.create_agent)
model = ChatLiteLLM(model='litellm_proxy/fixture-model',
    api_base='http://127.0.0.1:1/v1', api_key='synthetic-only')
for enabled in (False, True):
    middleware.clear()
    deepagents_options({'live_compaction': enabled})
    deepagents.create_deep_agent(model=model)
    assert len(middleware) >= 2
    assert all(('SummarizationMiddleware' in names) is not enabled for names in middleware)
    assert all({'FilesystemMiddleware', 'PatchToolCallsMiddleware'} <= names for names in middleware)
    assert any('SubAgentMiddleware' in names for names in middleware)
try:
    deepagents_options({'live_compaction': False})
except RuntimeError as error:
    assert 'Restart the runtime' in str(error)
else:
    raise AssertionError('Lost capability must require a fresh runtime')
"""], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


BACKGROUND_STEPS = 32
BACKGROUND_RECEIPT_REPEATS = 4000


def background_chat_response(body, message, sequence):
    finish = 'tool_calls' if message.get('tool_calls') else 'stop'
    common = {'id': f'background-{sequence}', 'created': 1, 'model': body['model']}
    if not body.get('stream'):
        return httpx.Response(200, json={**common, 'object': 'chat.completion',
            'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': len(json.dumps(body)) // 4, 'completion_tokens': 50, 'total_tokens': 100}})
    if message.get('tool_calls'):
        message['tool_calls'][0]['index'] = 0
    chunks = [{**common, 'object': 'chat.completion.chunk', 'choices': [choice]} for choice in (
        {'index': 0, 'delta': message, 'finish_reason': None},
        {'index': 0, 'delta': {}, 'finish_reason': finish})]
    return httpx.Response(200, content=''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
        + 'data: [DONE]\n\n', headers={'Content-Type': 'text/event-stream'})


def background_litellm_worker():
    """One native process, matching the lifetime of public SDK profile settings."""
    import os
    import sys
    from types import SimpleNamespace
    import litellm
    from sandbox import litellm_harness
    from sandbox.context_store import ContextStore
    from sandbox.harness_registry import create_agent
    job = json.load(sys.stdin)
    directory = Path(job['directory'])
    os.environ['WORKSPACE_RUN_TOKEN'] = job['capability']
    events, native_sessions = [], []
    def background_step(step: int, tail: str) -> str:
        """Record one completed fixture step and return its complete receipt."""
        with (directory / 'executions.txt').open('a') as output:
            output.write(str(step) + '\n')
        return f'receipt-{step} ' + tail + ' x' * BACKGROUND_RECEIPT_REPEATS
    binding = litellm_harness.RUNTIME_BINDINGS[job['harness']]
    litellm_harness.RUNTIME_BINDINGS[job['harness']] = SimpleNamespace(
        sandbox_factory=binding.sandbox_factory, in_process=binding.in_process,
        instructions=binding.instructions, options_factory=binding.options_factory,
        tools=lambda cwd, config: [background_step] if binding.in_process else [])
    factory = litellm.aagent_session
    def capture(*args, **kwargs):
        session = factory(*args, **kwargs)
        native_sessions.append(session)
        return session
    litellm.aagent_session = capture
    relay = BrokerRelay(job['remote'], job['capability']).start()
    store = ContextStore(directory.parent / 'live-context.sqlite3', job['run_id'])
    store.initialize([])
    agent = create_agent(job['harness'], spec={'model': job['model'], 'timeout': 90, 'max_iterations': 50},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'sandbox/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': job['capability']}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(('start', args[0])),
            complete=lambda *args: events.append(('complete', args[0])), commentary=lambda text: None),
        step=lambda: None, cwd=str(directory), context_store=store)
    try:
        result = agent.run_conversation('Perform each fixture step once, preserving all new results.',
            conversation_history=[], system_message='Execute the requested fixture tools and retain their receipts.')
        print('BACKGROUND_PROOF ' + json.dumps({'completed': result['completed'],
            'final_response': result['final_response'], 'sessions': len(native_sessions), 'events': events,
            'pending': bool(store.pending or agent.journal.pending),
            'summary_visible': job['summary_marker'] in ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))}))
    finally:
        agent.close()
        store.close()
        relay.close()


@pytest.mark.parametrize('harness', ['opencode', 'deepagents', 'tool-loop'])
def test_real_litellm_background_compaction_preserves_native_tools(workspace, tmp_path, monkeypatch, harness):
    """Pinned runtimes keep executing tools through the real broker while summarizing."""
    pytest.importorskip('litellm.harness')
    import importlib.metadata
    import shlex
    import shutil
    import subprocess
    import sys
    import litellm
    from sandbox.harness_dependencies import LITELLM_REVISION, runtime_version
    from test_codex_sdk_transport import background_gateway

    revision = subprocess.check_output(['git', '-C', str(Path(litellm.__file__).parent.parent),
                                       'rev-parse', 'HEAD'], text=True).strip()
    assert revision == LITELLM_REVISION
    if harness == 'opencode':
        if not shutil.which('opencode'):
            pytest.skip('Pinned OpenCode binary is not installed')
        assert runtime_version('opencode') == '1.18.35'
    if harness == 'deepagents':
        pytest.importorskip('deepagents')
        assert importlib.metadata.version('deepagents') == '0.7.22'
        assert importlib.metadata.version('langchain-litellm') == '0.11.0'
    directory = tmp_path / 'live-tools'
    directory.mkdir()
    executions = directory / 'executions.txt'
    steps = 0

    def upstream(body, state):
        nonlocal steps
        names = {tool.get('function', {}).get('name') for tool in body.get('tools', [])}
        tool_name = 'bash' if harness == 'opencode' else 'background_step'
        message = {'role': 'assistant', 'content': 'background-tools-ok'}
        if tool_name in names and steps < BACKGROUND_STEPS:
            steps += 1
            arguments = {'step': steps, 'tail': state.tail_marker}
            if harness == 'opencode':
                script = ('import pathlib; pathlib.Path("executions.txt").open("a").write('
                    + repr(str(steps) + '\n') + '); print(' + repr(f'receipt-{steps} ' + state.tail_marker)
                    + f" + ' x' * {BACKGROUND_RECEIPT_REPEATS})")
                arguments = {'command': shlex.quote(sys.executable) + ' -c ' + shlex.quote(script),
                             'description': f'Write receipt {steps}'}
            message['content'] = None
            message['tool_calls'] = [{'id': f'background-{steps}', 'type': 'function',
                'function': {'name': tool_name, 'arguments': json.dumps(arguments)}}]
        return background_chat_response(body, message, len(state.requests))

    with background_gateway(tmp_path, monkeypatch, workspace, harness, upstream) as state:
        job = {'harness': harness, 'directory': str(directory), 'remote': state.relay.remote,
            'capability': state.capability, 'model': state.app.state.settings.agent_model,
            'run_id': state.run['id'], 'summary_marker': state.summary_marker}
        script = 'import sys; sys.path.insert(0, ' + repr(str(Path(__file__).parent)) + "); from test_harnesses import background_litellm_worker; background_litellm_worker()"
        result = subprocess.run([sys.executable, '-c', script], input=json.dumps(job),
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        proof = json.loads(next(line.removeprefix('BACKGROUND_PROOF ') for line in result.stdout.splitlines()
                                if line.startswith('BACKGROUND_PROOF ')))
        assert proof['completed'], (proof, len(state.requests), len(state.summaries), state.held_calls, len(state.projections))
        assert proof['sessions'] == 1, 'Compaction restarted the native session'
        assert state.held_calls >= 2 and state.projections and not state.faults
        assert state.tail_marker not in json.dumps(state.summaries[0])
        assert executions.read_text().splitlines() == [str(index) for index in range(1, BACKGROUND_STEPS + 1)]
        started = [call for event, call in proof['events'] if event == 'start']
        completed = [call for event, call in proof['events'] if event == 'complete']
        assert len(started) == len(set(started)) == BACKGROUND_STEPS and sorted(started) == sorted(completed)
        assert not proof['pending'] and not proof['summary_visible']
        assert state.summary_marker not in json.dumps(state.original)
        print(f'{harness}: {len(state.requests)} foreground calls, {state.held_calls} while summary held, '
              f'{len(state.projections)} projected calls, {BACKGROUND_STEPS} tool receipts, one native session')


@pytest.mark.parametrize('harness', ['opencode', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('boundary', ['startup', 'later_stream'])
def test_real_litellm_first_request_fence_retains_each_prompt(tmp_path, monkeypatch, harness, boundary):
    """Optional pinned SDK contract; inference is scripted locally, never external."""
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    sdk = pytest.importorskip('litellm.harness')
    import importlib.metadata
    import shutil
    import subprocess
    from types import SimpleNamespace
    import litellm
    from sandbox import litellm_harness
    from sandbox.context_store import ContextStore
    from sandbox.harness_dependencies import LITELLM_REVISION, runtime_version
    from sandbox.harness_registry import create_agent
    revision = subprocess.run(['git', '-C', str(Path(litellm.__file__).parent.parent), 'rev-parse', 'HEAD'],
        capture_output=True, text=True, check=True).stdout.strip()
    assert revision == LITELLM_REVISION
    if harness == 'deepagents':
        pytest.importorskip('deepagents')
        pytest.importorskip('langchain_litellm')
        assert importlib.metadata.version('deepagents') == '0.7.22'
        assert importlib.metadata.version('langchain-litellm') == '0.11.0'
    if harness == 'opencode':
        if not shutil.which('opencode'):
            pytest.skip('Pinned OpenCode binary is not installed')
        assert runtime_version('opencode') == '1.18.35'
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'synthetic-only')
    monkeypatch.setattr(litellm_harness, 'prepare_runtime', lambda: None)
    monkeypatch.setattr(litellm_harness, 'prepare_binary', lambda binding: None)
    binding = litellm_harness.RUNTIME_BINDINGS[harness]
    monkeypatch.setitem(litellm_harness.RUNTIME_BINDINGS, harness, SimpleNamespace(
        sandbox_factory=binding.sandbox_factory, in_process=binding.in_process, instructions=binding.instructions,
        options_factory=(lambda config: sdk.OpenCodeOptions()) if harness == 'opencode' else binding.options_factory,
        tools=lambda cwd, config: []))
    requests, fences, native_ids = [], [], []
    session_factory = litellm.aagent_session
    def capture_session(*args, **kwargs):
        session = session_factory(*args, **kwargs)
        astream = session.astream
        def capture_stream(prompt):
            native_ids.append(session.state().native_session_id)
            return astream(prompt)
        session.astream = capture_stream
        return session
    monkeypatch.setattr(litellm, 'aagent_session', capture_session)
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            assert self.path == '/context/window'
            self.reply({'input_budget': 100000})
        def do_POST(self):
            value = json.loads(unseal('synthetic-only', self.path, self.rfile.read(int(self.headers['Content-Length']))))
            if self.path == '/context/maintenance':
                return self.reply({})
            if self.path == '/context/native':
                return self.reply({'lease': value['lease'], 'saved': True, 'state': None})
            assert self.path == '/v1/chat/completions'
            requests.append(value)
            if boundary == 'later_stream' and len(requests) == 1:
                assert agent.accept_input({'id': 1, 'content': 'FIRST_CORRECTION'})
            common = {'id': 'synthetic-response', 'created': 1, 'model': 'gpt-4o-mini'}
            if not value.get('stream'):
                return self.reply({**common, 'object': 'chat.completion', 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': 'Synthetic answer'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
            chunks = [{**common, 'object': 'chat.completion.chunk', 'choices': [choice]} for choice in (
                {'index': 0, 'delta': {'role': 'assistant', 'content': 'Synthetic answer'}, 'finish_reason': None},
                {'index': 0, 'delta': {}, 'finish_reason': 'stop'})]
            self.reply((''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
                + 'data: [DONE]\n\n').encode(), 'text/event-stream')
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    relay = BrokerRelay(f'http://127.0.0.1:{edge.server_port}', 'synthetic-only').start()
    store = ContextStore(tmp_path / 'session/context.sqlite3', 'synthetic-run')
    store.initialize([])
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    agent = create_agent(harness, spec={'model': 'gpt-4o-mini', 'timeout': 30, 'max_iterations': 4}, relay=relay,
        config={}, activity=SimpleNamespace(commentary=lambda text: None), step=lambda: None,
        cwd=str(workspace), context_store=store)
    before_model = relay.before_model
    injected = False
    def fence(request=None):
        nonlocal injected
        expected_admissions = 0 if boundary == 'startup' else 1
        if not injected and agent.model_calls == expected_admissions:
            injected = True
            assert agent.accept_input({'id': expected_admissions + 1,
                'content': 'FIRST_CORRECTION' if boundary == 'startup' else 'SECOND_CORRECTION'})
        try:
            return before_model(request)
        except InputPending:
            fences.append(agent.model_calls)
            raise
    relay.before_model = fence
    try:
        result = agent.run_conversation('ORIGINAL_TASK', conversation_history=[], system_message='Synthetic local fixture')
        assert result['completed'] and fences == ([0] if boundary == 'startup' else [1])
        assert len(requests) == agent.model_calls == (1 if boundary == 'startup' else 2)
        admitted_history = json.dumps(requests[-1]['messages'])
        expected = ['ORIGINAL_TASK', 'FIRST_CORRECTION'] + (['SECOND_CORRECTION'] if boundary == 'later_stream' else [])
        assert all(marker in admitted_history for marker in expected)
        assert all(json.dumps(result['messages']).count(marker) == 1 for marker in expected)
        if harness == 'opencode':
            assert native_ids[0] is None and native_ids[1]
            assert all(value == native_ids[1] for value in native_ids[1:])
        assert not relay.model_failed and not relay.last_failure and not relay.last_error
    finally:
        agent.close()
        relay.close()
        store.close()
        edge.shutdown()
        edge.server_close()


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
    reporter = ActivityReporter(lambda *args: events.append(args), tracing=True, omit_private_tool_payloads=True)
    reporter.start('one', name, {'content': 'private note'})
    reporter.complete('one', name, {'content': 'private note'}, 'private note')
    assert 'private note' not in json.dumps(events)


@pytest.fixture
def native_cli_runtime(tmp_path, monkeypatch):
    """Exercise the real adapter/journal/storage with the public LiteLLM API boundary."""
    import sys
    from types import SimpleNamespace
    from app.native_sessions import NativeSessions
    from sandbox import litellm_harness
    from sandbox.context_store import ContextStore
    from sandbox.harness_registry import create_agent

    calls, host, requests = [], {}, []
    class State(SimpleNamespace):
        @classmethod
        def loads(cls, value):
            return cls(**json.loads(value))
        def dumps(self):
            return json.dumps(vars(self)).encode()
    class Sandbox:
        def __init__(self, cwd, config, native=None):
            self.native, self.cwd = native, cwd
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, value, *, resumed, **options):
            self.options, self.resumed = options, resumed
            self.saved = value if resumed else State(harness=value, native_session_id=f'native-{len(calls) + 1}',
                workdir=options['sandbox'].cwd, model=options['model'].removeprefix('litellm_proxy/'), version=1)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def state(self): return self.saved
        def astream(self, prompt):
            calls.append({'resumed': self.resumed, 'prompt': prompt, 'session': self, **self.options})
            native = self.options['sandbox'].native
            if native is not None:
                path = native.cache / self.saved.harness / 'sessions' / 'native.jsonl'
                if self.resumed:
                    assert path.read_text() == 'native transcript'
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('native transcript')
            if host.get('during_turn'):
                host['during_turn']()
            return Stream()
    class Stream:
        result = SimpleNamespace(stop_reason='done', text='Completed once')
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
    harness = SimpleNamespace(CODEX='codex', OPENCODE='opencode', DEEPAGENTS='deepagents', TOOL_LOOP='tool-loop')
    api = SimpleNamespace(Harness=harness,
        aagent_session=lambda value, **kwargs: Session(value, resumed=False, **kwargs),
        aagent_resume=lambda value, **kwargs: Session(value, resumed=True, **kwargs))
    events = {name: type(name, (), {}) for name in ('Text', 'Reasoning', 'ToolCall', 'ToolResult', 'Approval')}
    monkeypatch.setitem(sys.modules, 'litellm', api)
    monkeypatch.setitem(sys.modules, 'litellm.harness', SimpleNamespace(State=State, StateIncompatible=RuntimeError, **events))
    for name, binding in litellm_harness.RUNTIME_BINDINGS.items():
        monkeypatch.setitem(litellm_harness.RUNTIME_BINDINGS, name, SimpleNamespace(sandbox_factory=Sandbox,
            options_factory=lambda config: None, tools=lambda cwd, config: [], in_process=binding.in_process,
            instructions=binding.instructions))
    monkeypatch.setattr(litellm_harness.LiteLLMAgent, 'validate', lambda agent: setattr(agent, 'runtime_version', 'fixture-cli 1.0'))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'first-capability')
    store = ContextStore(tmp_path / 'context.sqlite3', 'native-run')
    store.initialize([{'role': 'user', 'content': 'Original constraint: keep the red button.'}])
    def exchange(body):
        NativeSessions.validate(None, body)
        requests.append(json.loads(json.dumps(body)))
        if body['action'] in {'begin', 'restart'}:
            host['begins'] = host.get('begins', 0) + 1
            host['lease'] = body['lease']
            if body['action'] == 'restart':
                host.pop('state', None)
            return {'lease': body['lease'], 'state': host.get('state')}
        if body['lease'] != host.get('lease'):
            return {'lease': body['lease'], 'saved': False, 'reason': 'stale_lease'}
        if body['action'] == 'commit':
            host['state'] = json.loads(json.dumps(body['state']))
        if body['action'] == 'invalidate':
            host.pop('state', None)
            host['lease'] = ''
        return {'lease': body['lease'], 'saved': body['action'] == 'commit'}
    def create(kind):
        relay = SimpleNamespace(url='http://fresh-relay', native=exchange)
        return create_agent(kind, spec={'model': 'model-one', 'timeout': 10}, relay=relay,
            config={}, activity=SimpleNamespace(commentary=lambda text: None), step=lambda: None,
            cwd=str(tmp_path), context_store=store)
    yield SimpleNamespace(create=create, store=store, calls=calls, host=host, stream=Stream, requests=requests)
    store.close()


@pytest.mark.parametrize('harness', ['opencode', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('arrival', ['boundary', 'final'])
def test_litellm_corrections_keep_session_and_tool_receipts(native_cli_runtime, harness, arrival):
    runtime = native_cli_runtime
    agent = runtime.create(harness)
    def turn():
        assert agent.before_model()
        if len(runtime.calls) == 1:
            agent.journal.tool_started('write-once', 'Write', {})
            agent.journal.tool_finished('write-once', 'Saved write receipt')
            assert agent.accept_input({'id': 101, 'content': 'Use the saved receipt'})
            assert agent.accept_input({'id': 102, 'content': 'Answer concisely'})
            if arrival == 'boundary':
                with pytest.raises(InputPending):
                    agent.before_model()
                runtime.stream.result.stop_reason = 'runtime_error'
        else:
            assert runtime.calls[-1]['session'] is runtime.calls[0]['session']
            runtime.stream.result.stop_reason = 'done'
            runtime.stream.result.text = 'Corrected answer'
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert result['completed'] and result['final_response'] == 'Corrected answer'
        assert len(runtime.calls) == agent.model_calls == 2
        assert runtime.calls[-1]['prompt'].index('Use the saved receipt') < runtime.calls[-1]['prompt'].index('Answer concisely')
        assert agent.journal.completed_tools == 1
        saved = json.dumps(runtime.store.history())
        assert saved.count('Use the saved receipt') == saved.count('Answer concisely') == 1
        assert 'Saved write receipt' in saved
        assert not agent.accept_input({'id': 103, 'content': 'Too late'})
    finally:
        agent.close()


def test_litellm_later_model_request_cannot_erase_stream_input_fence(native_cli_runtime):
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    def turn():
        assert agent.before_model()
        if len(runtime.calls) == 1:
            assert agent.accept_input({'id': 1, 'content': 'Keep the correction'})
            with pytest.raises(InputPending):
                agent.before_model()
            # Another request can remain admissible while public tool receipts
            # catch up. It must not erase this stream's earlier local fence.
            agent.journal.tool_started('other', 'Read', {})
            assert agent.before_model()
            agent.journal.tool_finished('other', 'Read receipt')
            runtime.stream.result.stop_reason = 'runtime_error'
        else:
            runtime.stream.result.stop_reason = 'done'
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Task', conversation_history=[], system_message='Rules')
        assert result['completed'] and len(runtime.calls) == 2
        assert 'Keep the correction' in runtime.calls[-1]['prompt']
        assert agent.model_calls == 3 and agent.journal.completed_tools == 1
    finally:
        agent.close()


def test_litellm_delayed_model_request_cannot_fence_replacement_stream(native_cli_runtime):
    from dataclasses import replace
    from sandbox.harness_agent import HarnessInputs, TurnJournal
    agent = native_cli_runtime.create('tool-loop')
    agent.journal = TurnJournal([], 'task')
    agent.inputs = HarnessInputs(agent.journal)
    assert agent.accept_input({'id': 1, 'content': 'correction'})
    original, replacement = agent.input_fence, threading.Event()
    agent.context = replace(agent.context, step=lambda: setattr(agent, 'input_fence', replacement))
    try:
        assert agent.before_model() is False
        assert not original.is_set() and not replacement.is_set()
        assert agent.model_calls == 0 and agent.inputs.pending
    finally:
        agent.close()


@pytest.mark.parametrize('harness', ['opencode', 'deepagents', 'tool-loop'])
@pytest.mark.parametrize('correction', [False, True])
def test_litellm_completed_answer_survives_expired_rotation(native_cli_runtime, harness, correction):
    from sandbox.continuation import RotationDeadline
    runtime = native_cli_runtime
    agent = runtime.create(harness)
    now = [0]
    rotation = RotationDeadline(1, clock=lambda: now[0])
    from dataclasses import replace
    agent.context = replace(agent.context, step=lambda: rotation.step(agent))
    def turn():
        if len(runtime.calls) == 1:
            assert agent.before_model()
            now[0] = 2  # Rotation expires while the model finishes its answer.
            if correction:
                assert agent.accept_input({'id': 101, 'content': 'Do the next step'})
        else:
            assert not agent.before_model()  # Still fence any unfinished follow-up.
            runtime.stream.result.stop_reason = 'runtime_error'
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert result['completed'] is not correction
        assert result['interrupted'] is correction and rotation.requested is correction
        assert len(runtime.calls) == (2 if correction else 1)
        assert 'Completed once' in json.dumps(runtime.store.history())
        if agent.native is not None:
            assert (agent.native.staged is not None) is (not correction)
    finally:
        agent.close()


@pytest.mark.parametrize('boundary', ['provider_failure', 'stop', 'pending_tool', 'model_cap'])
def test_litellm_corrections_do_not_override_terminal_boundaries(native_cli_runtime, boundary):
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    agent.context.spec['max_iterations'] = 1
    def turn():
        assert agent.before_model()
        assert agent.accept_input({'id': 101, 'content': 'Retain this correction'})
        if boundary == 'pending_tool':
            agent.journal.tool_started('pending', 'Write', {})
        elif boundary == 'stop':
            agent.interrupt()
        elif boundary == 'provider_failure':
            agent.context.relay.model_failed = True
            # A provider failure wins even when another request was fenced.
            agent.input_fence.set()
            runtime.stream.result.stop_reason = 'runtime_error'
        else:
            assert not agent.before_model() and agent.model_limit_reached
            runtime.stream.result.stop_reason = 'runtime_error'
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert not result['completed'] and len(runtime.calls) == 1
        assert 'Retain this correction' in json.dumps(runtime.store.history())
        assert not agent.accept_input({'id': 102, 'content': 'Too late'})
    finally:
        agent.close()


def test_litellm_final_admission_race_drains_before_completion(native_cli_runtime, monkeypatch):
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    def turn():
        if len(runtime.calls) != 1:
            return
        close_if_empty = agent.inputs.close_if_empty
        def race():
            agent.accept_input({'id': 101, 'content': 'Arrived at finalization'})
            return close_if_empty()
        monkeypatch.setattr(agent.inputs, 'close_if_empty', race)
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert result['completed'] and len(runtime.calls) == 2
        assert 'Arrived at finalization' in runtime.calls[-1]['prompt']
    finally:
        agent.close()


def test_litellm_corrections_share_model_call_limit(native_cli_runtime):
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    agent.context.spec['max_iterations'] = 2
    def turn():
        assert agent.before_model()
        assert agent.accept_input({'id': len(runtime.calls), 'content': 'Another correction'})
        if len(runtime.calls) == 2:
            assert not agent.before_model() and agent.model_limit_reached
            runtime.stream.result.stop_reason = 'runtime_error'
    runtime.host['during_turn'] = turn
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert not result['completed'] and len(runtime.calls) == agent.model_calls == 2
        assert json.dumps(runtime.store.history()).count('Another correction') == 2
    finally:
        agent.close()


def test_litellm_corrections_share_original_deadline(native_cli_runtime, monkeypatch):
    import asyncio
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    agent.context.spec['timeout'] = .15
    async def stream(self):
        await asyncio.sleep(.09)
        if len(runtime.calls) <= 2:
            agent.accept_input({'id': len(runtime.calls), 'content': 'Another correction'})
        raise StopAsyncIteration
    monkeypatch.setattr(runtime.stream, '__anext__', stream)
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert not result['completed'] and result['failed']
        assert len(runtime.calls) == 2 and 'deadline elapsed' in result['final_response']
        assert not agent.accept_input({'id': 103, 'content': 'Too late'})
    finally:
        agent.close()


def test_litellm_context_recovery_consumes_saved_corrections_once(native_cli_runtime):
    runtime = native_cli_runtime
    agent = runtime.create('tool-loop')
    old_inputs = []
    def turn():
        assert agent.before_model()
        if len(runtime.calls) == 1:
            assert agent.accept_input({'id': 101, 'content': 'Saved correction'})
            old_inputs.append(agent.inputs)
            agent.context.relay.context_required = {'input_tokens': 50000, 'input_budget': 20000}
            runtime.stream.result.stop_reason = 'runtime_error'
        elif len(runtime.calls) == 2:
            assert agent.inputs is not old_inputs[0] and not agent.inputs.pending
            assert runtime.calls[-1]['prompt'].count('Saved correction') == 1
            assert agent.accept_input({'id': 102, 'content': 'Later correction'})
            runtime.stream.result.stop_reason = 'done'
    def compact(previous, entries, **kwargs):
        assert old_inputs[0].closed
        assert not agent.accept_input({'id': 102, 'content': 'Later correction'})
        assert 'Saved correction' in json.dumps(entries)
        return 'Preserved original task and Saved correction'
    runtime.host['during_turn'] = turn
    agent.context.relay.compact = compact
    try:
        result = agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
        assert result['completed'] and len(runtime.calls) == 3
        assert runtime.calls[0]['session'] is not runtime.calls[1]['session']
        assert runtime.calls[1]['session'] is runtime.calls[2]['session']
        assert 'Later correction' in runtime.calls[2]['prompt'] and 'Saved correction' not in runtime.calls[2]['prompt']
        assert json.dumps(result['messages']).count('Saved correction') == 1
    finally:
        agent.close()


def test_native_cli_cold_resume_uses_saved_runtime_and_fresh_capability(native_cli_runtime, monkeypatch):
    runtime = native_cli_runtime
    first = runtime.create('opencode')
    assert first.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')['completed']
    assert 'Original constraint' in runtime.calls[0]['prompt']
    assert 'state' not in runtime.host  # Native upload belongs after answer delivery.
    first.close()
    assert runtime.host['state']['files'] and not list(first.native.cache.rglob('*'))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'replacement-capability')
    second = runtime.create('opencode')
    try:
        assert second.run_conversation('TLDR?', conversation_history=[], system_message='Rules')['completed']
        assert runtime.calls[-1]['resumed'] is True
        assert runtime.calls[-1]['prompt'] == 'TLDR?'
        assert runtime.calls[-1]['api_key'] == 'replacement-capability'
        assert runtime.calls[-1]['api_base'] == 'http://fresh-relay'
        assert runtime.store.db.execute("SELECT count(*) FROM journal WHERE preview LIKE '%TLDR?%'").fetchone()[0] == 1
    finally:
        second.close()


@pytest.mark.parametrize('compaction_fails', [False, True])
def test_native_cli_confirmed_rejection_rebuilds_before_saving(native_cli_runtime, compaction_fails):
    from sandbox.context_store import ContextUnavailable
    runtime = native_cli_runtime
    first = runtime.create('opencode')
    first.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
    first.close()
    agent = runtime.create('opencode')
    old_lease, completed_writes, compactions = [], [], []
    def reject():
        if len(runtime.calls) != 2:
            return
        assert runtime.calls[-1]['resumed']
        old_lease.append(agent.native.lease)
        agent.journal.tool_started('write-once', 'Write', {})
        completed_writes.append('write-once')
        agent.journal.tool_finished('write-once', 'Saved write receipt')
        agent.context.relay.context_required = {'input_tokens': 50000, 'input_budget': 20000}
        runtime.stream.result.stop_reason = 'runtime_error'
    def compact(previous, entries, **kwargs):
        assert not agent.native.enabled and agent.native.staged is None
        assert not list(agent.native.cache.rglob('*'))
        assert runtime.requests[-1]['action'] == 'invalidate'
        compactions.append(1)
        if compaction_fails:
            raise RuntimeError('Compaction unavailable')
        runtime.stream.result.stop_reason = 'done'
        return 'Original constraint: keep the red button. Saved write receipt. Do not repeat the write.'
    runtime.host['during_turn'] = reject
    agent.context.relay.compact = compact
    try:
        if compaction_fails:
            with pytest.raises(ContextUnavailable):
                agent.run_conversation('Continue the edit.', conversation_history=[], system_message='Rules')
        else:
            assert agent.run_conversation('Continue the edit.', conversation_history=[], system_message='Rules')['completed']
        assert 'state' not in runtime.host  # The replacement is uploaded only after the answer.
    finally:
        agent.close()
    assert compactions == [1] and completed_writes == ['write-once']
    if compaction_fails:
        assert len(runtime.calls) == 2 and 'state' not in runtime.host
        assert not any(body['action'] == 'restart' for body in runtime.requests)
        return
    assert runtime.host.get('state'), 'The recovered turn must save a new native checkpoint'
    assert agent.native.lease != old_lease[0]
    assert len(runtime.calls) == 3 and not runtime.calls[-1]['resumed']
    assert 'Saved write receipt' in runtime.calls[-1]['prompt']
    assert [body['action'] for body in runtime.requests] == ['begin', 'commit', 'begin', 'invalidate', 'restart', 'commit']
    third = runtime.create('opencode')
    try:
        assert third.run_conversation('TLDR?', conversation_history=[], system_message='Rules')['completed']
        assert runtime.calls[-1]['resumed'] and runtime.calls[-1]['prompt'] == 'TLDR?'
        assert completed_writes == ['write-once']
    finally:
        third.close()


@pytest.mark.parametrize('field,value', [('harness', 'codex'), ('workdir', '/other-workspace'),
    ('model', 'litellm_proxy/other-model'), ('native_session_id', None), ('native_session_id', '--last'),
    ('files', {}), ('files', {'../escape': 'eA=='})])
def test_incompatible_native_cli_state_falls_back_before_inference(native_cli_runtime, field, value):
    runtime = native_cli_runtime
    first = runtime.create('opencode')
    first.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
    first.close()
    saved = runtime.host['state']
    if field == 'files':
        saved[field] = value
    else:
        state = json.loads(saved['state'])
        state[field] = value
        saved['state'] = json.dumps(state)
    second = runtime.create('opencode')
    try:
        assert second.run_conversation('TLDR?', conversation_history=[], system_message='Rules')['completed']
        assert runtime.calls[-1]['resumed'] is False
        assert 'Original constraint' in runtime.calls[-1]['prompt'] and 'TLDR?' in runtime.calls[-1]['prompt']
    finally:
        second.close()
    assert runtime.host.get('state'), 'The successful fresh fallback must replace the rejected checkpoint'
    assert len(runtime.calls) == 2  # The rejected native state never started an SDK invocation.
    third = runtime.create('opencode')
    try:
        assert third.run_conversation('Next question', conversation_history=[], system_message='Rules')['completed']
        assert runtime.calls[-1]['resumed'] and runtime.calls[-1]['prompt'] == 'Next question'
    finally:
        third.close()


@pytest.mark.parametrize('failure', ['runtime_error', 'interrupted', 'pending_tool'])
def test_unfinished_native_cli_turn_cannot_publish_resume_state(native_cli_runtime, failure):
    runtime = native_cli_runtime
    agent = runtime.create('opencode')
    if failure == 'runtime_error':
        runtime.stream.result.stop_reason = 'runtime_error'
    elif failure == 'interrupted':
        runtime.host['during_turn'] = agent.interrupt
    else:
        runtime.host['during_turn'] = lambda: agent.journal.tool_started('pending', 'Write', {})
    try:
        agent.run_conversation('Finish the edit.', conversation_history=[], system_message='Rules')
    finally:
        agent.close()
    assert 'state' not in runtime.host
    assert len(runtime.calls) == 1 and not any(body['action'] == 'restart' for body in runtime.requests)


def test_goal_iteration_renews_native_contract_and_discards_intermediate_result(native_cli_runtime):
    runtime = native_cli_runtime
    agent = runtime.create('opencode')
    agent.run_conversation('First step.', conversation_history=[], system_message='Original rules')
    previous = agent.native
    assert previous.staged is not None and 'state' not in runtime.host
    runtime.stream.result.stop_reason = 'runtime_error'
    try:
        agent.run_conversation('Next step.', conversation_history=[], system_message='Updated rules')
        assert agent.native.lease != previous.lease
        assert agent.native.compatibility != previous.compatibility
    finally:
        agent.close()
    assert 'state' not in runtime.host


@pytest.mark.parametrize('kind', ['deepagents', 'tool-loop'])
def test_process_only_harnesses_keep_public_journal_recovery(native_cli_runtime, kind):
    runtime = native_cli_runtime
    agent = runtime.create(kind)
    try:
        assert agent.run_conversation('TLDR?', conversation_history=[], system_message='Rules')['completed']
        assert 'Original constraint' in runtime.calls[-1]['prompt']
        assert not runtime.calls[-1]['resumed'] and not runtime.host
    finally:
        agent.close()


async def test_native_cli_storage_is_scoped_for_setup_launch_and_temporary_files(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from sandbox.harness_bindings import local_sandbox
    class LocalSandbox:
        def __init__(self, cwd): self._tempdirs = []
        def _check_open(self): pass
        def child_env(self, env=None): return {'upstream_filter_applied': 'yes', **(env or {})}
    monkeypatch.setitem(sys.modules, 'litellm.harness.sandbox.local', SimpleNamespace(LocalSandbox=LocalSandbox))
    root = tmp_path / 'native'
    native = SimpleNamespace(root=root, env={'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
        'CODEX_HOME': str(root / 'codex'), 'XDG_DATA_HOME': str(root / 'data')})
    sandbox = local_sandbox(str(tmp_path), {}, native)
    setup = sandbox.child_env()
    assert setup['HOME'] == native.env['HOME'] and setup['upstream_filter_applied'] == 'yes'
    temporary = await sandbox.tempdir()
    assert Path(temporary).is_relative_to(root) and temporary in sandbox._tempdirs
    launch = sandbox.child_env({'CODEX_HOME': temporary, 'HOME': '/foreign', 'TMPDIR': '/foreign'})
    assert launch['CODEX_HOME'] == temporary and launch['HOME'] == native.env['HOME']
    assert launch['TMPDIR'] == native.env['TMPDIR']
    for key in ('CODEX_HOME', 'XDG_DATA_HOME', 'CLAUDE_CONFIG_DIR'):
        with pytest.raises(ValueError, match='escaped'):
            sandbox.child_env({key: '/foreign'})


def test_native_cli_version_comes_from_installed_binary_and_failure_disables_resume(monkeypatch):
    from types import SimpleNamespace
    from sandbox import harness_dependencies
    calls = []
    def version(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='1.18.35\n')
    monkeypatch.setattr(harness_dependencies.subprocess, 'run', version)
    assert harness_dependencies.runtime_version('opencode') == '1.18.35'
    assert calls == [['opencode', '--version']]
    monkeypatch.setattr(harness_dependencies.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    assert harness_dependencies.runtime_version('opencode') == ''
