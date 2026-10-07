import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from sandbox.claude_harness import ClaudeAgent
from sandbox.harness_agent import TurnJournal
from test_workspace import workspace
from test_slack import slack_app, event, signed


OPUS = 'anthropic/claude-opus-5-5'


def make_agent(monkeypatch, tmp_path, step=lambda: None):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'run-capability')
    events = []
    activity = SimpleNamespace(start=lambda *args: events.append(('start', args)),
                               complete=lambda *args: events.append(('complete', args)),
                               commentary=lambda text: events.append(('commentary', text)))
    agent = ClaudeAgent(spec={'model': OPUS, 'max_iterations': 6}, relay=SimpleNamespace(url='http://127.0.0.1:1234'),
                        config={'mcp_servers': {'workspace': {'command': 'python', 'args': ['mcp_bridge.py']}}},
                        activity=activity, step=step, cwd=str(tmp_path), definition=None)
    return agent, events


def test_default_selects_sdk_without_changing_the_configured_model():
    settings = Settings(_env_file=None, agent_model='openai/gpt-6-astra')
    assert settings.agent_harness == 'claude-agent-sdk'
    assert settings.harness_model(settings.agent_harness) == 'openai/gpt-6-astra'
    assert settings.harness_model(settings.agent_harness, 'astra') == 'openai/gpt-6-astra'
    with pytest.raises(ValueError, match='enabled'):
        settings.harness_model(settings.agent_harness, 'unconfigured/model')
    assert settings.harness_model('hermes', 'astra') == 'openai/gpt-6-astra'


def test_web_defaults_and_automation_persistence(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.agent_harness = 'claude-agent-sdk'
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    config = client.get('/api/config').json()
    assert (config['harness'], config['model']) == ('claude-agent-sdk', 'openai/gpt-6-astra')
    run = client.post('/api/runs', json={'prompt': 'Use the configured runtime'}).json()
    assert (run['harness'], run['model']) == ('claude-agent-sdk', 'openai/gpt-6-astra')
    automation = client.post('/api/automations', json={'definition': {
        'name': 'SDK workflow', 'prompt': 'Read the project', 'mode': 'demo'}}).json()
    assert automation['definition']['harness'] == 'claude-agent-sdk'
    launched = client.post(f"/api/automations/{automation['id']}/run", json={'revision': 1, 'client_id': 'sdk-launch'}).json()
    assert app.state.store.run(launched['run_id'])['harness'] == 'claude-agent-sdk'
    # A saved pre-migration workflow remains explicitly Hermes on launch/edit.
    from app.automations import Definition
    assert Definition(name='Old workflow', prompt='Read the project').harness == 'hermes'


def test_slack_defaults_to_sdk(slack_app):
    app, client, submitted, replies = slack_app
    app.state.settings.agent_harness = 'claude-agent-sdk'
    body = event(text='<@U99999999> Read the repository')
    assert client.post('/hooks/slack/events', **signed(body)).status_code == 200
    assert (submitted[0]['harness'], submitted[0]['model']) == ('claude-agent-sdk', app.state.settings.resolve_model())


def test_sdk_options_force_prompt_caching_and_broker_only(monkeypatch, tmp_path):
    monkeypatch.setenv('DISABLE_PROMPT_CACHING', '1')
    monkeypatch.setenv('ENABLE_TOOL_SEARCH', 'false')
    agent, _ = make_agent(monkeypatch, tmp_path)
    options = agent.options('stable system prompt')
    assert options.env['DISABLE_PROMPT_CACHING'] == '0'
    assert all(options.env['DISABLE_PROMPT_CACHING_' + family] == '0' for family in ('OPUS', 'SONNET', 'HAIKU'))
    assert options.env['ANTHROPIC_API_KEY'] == 'run-capability'
    assert options.env['ANTHROPIC_BASE_URL'] == agent.context.relay.url
    assert options.env['ENABLE_TOOL_SEARCH'] == 'true'
    assert 'ToolSearch' in options.tools and 'ToolSearch' in options.allowed_tools
    assert options.permission_mode == 'dontAsk'
    assert options.setting_sources == [] and options.strict_mcp_config
    assert set(options.mcp_servers) == {'moyai'}
    assert options.fallback_model is None


@pytest.mark.parametrize('installed', [None, '0.1.0', '0.2.163'])
def test_old_snapshots_prepare_only_the_pinned_sdk(monkeypatch, tmp_path, installed):
    from importlib.metadata import PackageNotFoundError
    agent, _ = make_agent(monkeypatch, tmp_path)
    installs = []
    def version(package):
        assert package == 'claude-agent-sdk'
        if installed is None:
            raise PackageNotFoundError(package)
        return installed
    monkeypatch.setattr('importlib.metadata.version', version)
    monkeypatch.setattr('sandbox.claude_harness.subprocess.run', lambda args, **kwargs: installs.append(args))
    agent.validate()
    assert len(installs) == (installed != '0.2.163')
    if installs:
        assert installs[0][3:] == ['install', 'claude-agent-sdk==0.2.163', 'mcp<2']


@pytest.mark.parametrize('stop', ['success', 'failure', 'interrupt'])
def test_sdk_tool_receipts_and_completion(monkeypatch, tmp_path, stop):
    from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ResultMessage
    agent, events = make_agent(monkeypatch, tmp_path)
    class Client:
        def __init__(self, *, options): self.options = options
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt):
            assert 'CURRENT REQUEST:\nfollow up' in prompt
        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock('Checking the file.'), ThinkingBlock('private reasoning', 'sig')], model=OPUS)
            event = {'tool_name': 'Read', 'tool_input': {'file_path': '/workspace/proof.py'}, 'tool_use_id': 'read1'}
            await agent.tool_hook({**event, 'hook_event_name': 'PreToolUse'}, 'read1', {})
            assert agent.journal.pending == {'read1'}
            await agent.tool_hook({**event, 'hook_event_name': 'PostToolUse', 'tool_response': 'print(42)'}, 'read1', {})
            if stop == 'interrupt': agent.interrupt()
            if stop == 'failure': raise RuntimeError('secret provider payload')
            yield ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id='sdk-session', result='The file prints 42.')
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    history = [{'role': 'user', 'content': 'previous task'}, {'role': 'assistant', 'content': 'previous answer'}]
    result = agent.run_conversation('follow up', conversation_history=history, system_message='stable')
    assert result['completed'] == (stop == 'success')
    assert result['interrupted'] == (stop == 'interrupt')
    assert result['failed'] == (stop == 'failure')
    assert not agent.journal.pending
    assert result['messages'][2]['content'] == 'follow up'
    assert any(m.get('role') == 'tool' and m['content'] == 'print(42)' for m in result['messages'])
    assert 'private reasoning' not in json.dumps(events)
    assert 'secret provider payload' not in json.dumps(result)
    assert events[0] == ('commentary', 'Checking the file.')
    agent.close()
    assert agent.context.relay.before_model is None


def test_checkpoint_waits_for_all_parallel_tool_receipts(monkeypatch, tmp_path):
    agent, _ = make_agent(monkeypatch, tmp_path)
    agent.journal = TurnJournal([], 'task')
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
                                           agent.context.activity, agent.interrupt, agent.context.cwd)
    for id in ('a', 'b'): agent.journal.tool_started(id, 'Read', {})
    agent.journal.tool_finished('a', 'first')
    assert agent.before_model()
    agent.journal.tool_finished('b', 'second')
    assert not agent.before_model()


@pytest.mark.parametrize('mirror', ['complete', 'dropped', 'missing'])
def test_incomplete_native_mirror_never_becomes_a_new_checkpoint(monkeypatch, tmp_path, mirror):
    from uuid import uuid4
    from claude_agent_sdk import ResultMessage, SystemMessage
    from sandbox.context_store import ContextStore
    agent, _ = make_agent(monkeypatch, tmp_path)
    agent.context_store = ContextStore(tmp_path / 'context.db', 'run')
    agent.context_store.initialize([])
    session_id, actions = str(uuid4()), []
    key = {'session_id': session_id, 'project_key': 'fixture'}
    saved = {'session_id': session_id, 'records': [{'key': key,
        'entries': [{'type': 'assistant', 'uuid': 'older', 'message': {'role': 'assistant', 'content': 'Old native answer'}}]}]}
    def native(body):
        actions.append(body['action'])
        return {'lease': body['lease'], 'state': saved}
    agent.context.relay.native = native
    class Client:
        def __init__(self, *, options): self.options = options
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt):
            assert prompt == 'new question'
        async def receive_response(self):
            if mirror != 'missing':
                await self.options.session_store.append(key, [{'type': 'user', 'uuid': 'new',
                    'message': {'role': 'user', 'content': 'new question'}}])
            if mirror == 'dropped':
                yield SystemMessage(subtype='mirror_error', data={})
            yield ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id=session_id, result='New answer still succeeds')
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    try:
        result = agent.run_conversation('new question', conversation_history=[], system_message='stable')
        assert result['completed'] and result['final_response'] == 'New answer still succeeds'
        agent.close()
        assert ('commit' in actions) == (mirror == 'complete')
    finally:
        agent.context_store.close()
