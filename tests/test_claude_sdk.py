import asyncio
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from app.config import Settings
from sandbox.claude_harness import ClaudeAgent
from sandbox.harness_agent import TurnJournal
from test_workspace import workspace
from test_slack import slack_app, event, signed


OPUS = 'anthropic/claude-opus-5-5'


def make_agent(monkeypatch, tmp_path, step=lambda: None, **spec):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'run-capability')
    events = []
    activity = SimpleNamespace(start=lambda *args: events.append(('start', args)),
                               complete=lambda *args: events.append(('complete', args)),
                               commentary=lambda text: events.append(('commentary', text)),
                               emit=lambda *args: events.append(args))
    agent = ClaudeAgent(spec={'model': OPUS, 'max_iterations': 6, **spec}, relay=SimpleNamespace(url='http://127.0.0.1:1234'),
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
        async def receive_messages(self):
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


@pytest.mark.parametrize('delivery', ['merged', 'next-turn', 'write-error', 'missing-echo', 'subagent-echo', 'echo-before-write'])
def test_active_input_waits_for_its_native_echo_and_preserves_failed_input(monkeypatch, tmp_path, delivery):
    import asyncio
    from claude_agent_sdk import ResultMessage, UserMessage
    agent, _ = make_agent(monkeypatch, tmp_path)
    sent = []

    class Client:
        def __init__(self, *, options):
            assert options.extra_args['replay-user-messages'] is None
            self.ready = asyncio.Event()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt):
            if isinstance(prompt, str):
                assert agent.before_model()
                return
            async for envelope in prompt:
                sent.append(envelope)
            self.ready.set()
            if delivery == 'write-error':
                raise RuntimeError('private native transport error')
            if delivery == 'echo-before-write':
                await asyncio.sleep(.05)
                sent[0]['write_complete'] = True
        async def receive_messages(self):
            assert agent.accept_input({'id': 17, 'content': 'Keep the original task and change the color.'})
            await asyncio.wait_for(self.ready.wait(), 1)
            if delivery == 'write-error':
                await asyncio.sleep(2)
                return
            def result(text):
                return ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id='sdk-session', result=text)
            if delivery == 'next-turn':
                yield result('The first response finished while input was in flight.')
            # Other native user/tool messages cannot settle this correction.
            yield UserMessage(content='unrelated', uuid='unrelated')
            if delivery != 'missing-echo':
                yield UserMessage(content=sent[0]['message']['content'], uuid=sent[0]['uuid'],
                                  parent_tool_use_id='subagent' if delivery == 'subagent-echo' else None)
            assert agent.before_model()
            yield result('The original task now uses the requested color.')

    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    result = agent.run_conversation('Complete the original task.', conversation_history=[], system_message='stable')
    assert result['completed'] == (delivery in {'merged', 'next-turn', 'echo-before-write'})
    assert result['failed'] == (delivery in {'write-error', 'missing-echo', 'subagent-echo'})
    if result['completed']:
        assert result['final_response'] == 'The original task now uses the requested color.'
    assert len(sent) == 1 and sent[0]['origin'] == {'kind': 'human'}
    if delivery == 'echo-before-write':
        assert sent[0].get('write_complete'), 'Finalization must join the native input write before closing it'
    assert sum('change the color' in str(message.get('content')) for message in result['messages']) == 1
    assert not agent.accept_input({'id': 18, 'content': 'Too late for this SDK invocation.'})
    assert 'private native' not in json.dumps(result)
    agent.close()


def test_claude_model_cap_counts_all_queries_in_one_invocation(monkeypatch, tmp_path):
    agent, _ = make_agent(monkeypatch, tmp_path)
    agent.context.spec['max_iterations'] = 2
    agent.journal = TurnJournal([], 'task')
    assert agent.before_model() and agent.before_model()
    assert not agent.before_model()
    assert agent.model_calls == 2 and agent.boundary_failed
    assert agent.boundary_reason == 'model call limit reached'


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
        async def receive_messages(self):
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


@pytest.mark.parametrize('source', ['http', 'result', 'exception', 'cleanup'])
def test_claude_failure_metadata_replaces_private_result_and_stderr(monkeypatch, tmp_path, source):
    from claude_agent_sdk import ResultMessage, ProcessError
    from sandbox.activity import ActivityReporter
    agent, _ = make_agent(monkeypatch, tmp_path)
    events = []
    activity = ActivityReporter(lambda *args: events.append(args))
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
        activity, agent.context.step, agent.context.cwd)
    agent.context.relay.last_failure = {'http_status': 502, 'request_id': 'broker-request-123',
                                       'body': 'private-broker-body'}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args):
            if source == 'cleanup':
                raise ProcessError('private-process-body', exit_code=2, stderr='private-stderr')
        async def query(self, prompt): pass
        async def receive_messages(self):
            if source == 'exception':
                raise ProcessError('private-process-body', exit_code=2, stderr='private-stderr')
            yield ResultMessage(subtype='error_during_execution' if source == 'result' else 'success',
                duration_ms=1, duration_api_ms=1, is_error=source != 'cleanup', num_turns=1,
                session_id='session', result='private-result-body', errors=['private-errors-body'],
                api_error_status=502 if source == 'http' else None)
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    result = agent.run_conversation('Read', conversation_history=[], system_message='Moyai')
    failure = result['sdk_failure']
    assert result['failed'] and not result['completed']
    assert failure['broker_request_id'] == 'broker-request-123' and failure['http_status'] == 502
    if source in {'exception', 'cleanup'}:
        assert failure['exception_type'] == 'ProcessError' and failure['exit_code'] == 2
    else:
        assert failure['error_count'] == 1
    assert len([event for event in events if event[0] == 'error']) == 1
    assert 'private-' not in json.dumps([result, events])


def recovery_client(monkeypatch, agent, stream, *, hold_continuation=False):
    from test_codex_sdk import install_transport_relay
    relay, resumed = install_transport_relay(agent)
    agent.journal = TurnJournal([], 'Work', agent.context_store)
    observed = SimpleNamespace(queries=[], envelopes=[], exits=[], release_query=asyncio.Event())

    class Client:
        def __init__(self, *, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): observed.exits.append(True)
        async def query(self, prompt):
            if not isinstance(prompt, str):
                envelopes = [envelope async for envelope in prompt]
                observed.envelopes.extend(envelopes)
                prompt = '\n'.join(envelope['message']['content'] for envelope in envelopes)
            observed.queries.append(prompt)
            assert agent.before_model(json.dumps({'messages': [{'content': prompt}]}).encode())
            if hold_continuation and len(observed.queries) > 1:
                await observed.release_query.wait()
        async def receive_messages(self):
            async for message in stream():
                yield message

    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    return relay, resumed, observed


def recovery_result(failed: bool = True):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype='success', api_error_status=502 if failed else None,
        duration_ms=1, duration_api_ms=1, is_error=failed, num_turns=1,
        session_id='same-native-session', result='Finished with the existing work.' if not failed else '')


def test_claude_recovery_admission_waits_for_continuation_without_spending_budget(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sandbox.broker_relay import InputPending
    steps = []
    def checkpoint_step() -> None:
        steps.append(True)
    agent, _ = make_agent(monkeypatch, tmp_path, step=checkpoint_step)
    agent.journal = TurnJournal([], 'Work')
    blocked = agent.context.relay.on_model_blocked()
    assert isinstance(blocked, InputPending)
    agent.recovery_input = 'continuation-identity'
    receipts = [blocked.receipt]
    for request in (None, b'{"messages": [{"content": "Original command completed"}]}'):
        with pytest.raises(InputPending) as pending:
            agent.before_model(request)
        receipts.append(pending.value.receipt)
    assert len(set(receipts)) == 3 and all(len(receipt) == 32 for receipt in receipts)
    assert agent.model_calls == 0 and steps == [] and agent.recovery_rejections == set(receipts)
    assert agent.recovery_input == 'continuation-identity' and not agent.boundary_failed
    assert agent.before_model(b'{"messages": [{"content": "Recovery input: continuation-identity"}]}')
    assert agent.recovery_input is None and agent.model_calls == 1 and steps == [True]
    assert agent.recovery_rejections == set(receipts), 'Admission cannot erase already-issued local rejections'
    agent.close()
    assert agent.context.relay.before_model is None and agent.context.relay.on_model_blocked is None


@pytest.mark.parametrize('query_pending', [False, True], ids=['query-returned', 'final-before-query-return'])
@pytest.mark.parametrize('source', ['admission', 'relay'])
def test_claude_late_local_rejection_cannot_stop_an_admitted_continuation(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, query_pending: bool, source: str) -> None:
    from dataclasses import replace
    from sandbox.broker_relay import InputPending
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path, max_iterations=2)

    async def stream():
        model_failure(relay)['route'] = '/v1/messages'
        if source == 'relay':
            assert agent.recovery_input is None and relay.model_failed
            assert relay.on_model_blocked().receipt in agent.recovery_rejections
        yield recovery_result()
        while len(observed.queries) == 1:
            await asyncio.sleep(.005)
        assert agent.recovery_input is None and len(agent.recovery_rejections) == 1
        assert relay.last_failure is None and not relay.model_failed
        # The stale background turn's local rejection can arrive after the new input
        # passed admission; retain its identity until this result is consumed.
        receipt = next(iter(agent.recovery_rejections))
        yield replace(recovery_result(), subtype='success', api_error_status=400,
                      result='API Error: 400 ' + InputPending(receipt).message())
        yield recovery_result(False)
        # The SDK may deliver its final response while query() is still writing.
        # Release that write only after the receive loop has read the result.
        observed.release_query.set()

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream, hold_continuation=query_pending)
    resume = relay.resume_model

    def reopen(observed_failure: dict, *, live: bool = False) -> bool:
        assert resume(observed_failure, live=live)
        assert agent.recovery_input
        if source == 'admission':
            with pytest.raises(InputPending):
                agent.before_model(b'{"messages": []}')
        assert agent.model_calls == 1
        return True

    relay.resume_model = reopen
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert result['completed'] and not agent.recovery_rejections
    assert agent.model_calls == 2 and agent.transport_attempt == 1
    assert len(observed.queries) == 2 and len(resumed) == 1 and observed.exits == [True]


@pytest.mark.parametrize('reason', ['unmatched', 'missing-envelope', 'ordinary400', 'ordinary409',
                                   'wrong-status', 'already-consumed', 'stop', 'model-limit'])
def test_claude_local_rejection_receipts_cannot_hide_terminal_results(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str) -> None:
    from dataclasses import replace
    from sandbox.broker_relay import InputPending
    agent, _ = make_agent(monkeypatch, tmp_path)

    async def stream():
        agent.recovery_input = 'continuation'
        with pytest.raises(InputPending) as pending:
            agent.before_model(b'{"messages": []}')
        assert agent.before_model(b'{"messages": ["continuation"]}')
        receipt = pending.value.receipt
        rejection = replace(recovery_result(), api_error_status=400,
                            result='API Error: 400 ' + InputPending(receipt).message())
        if reason == 'already-consumed':
            yield rejection
        if reason == 'unmatched':
            rejection = replace(rejection, result='API Error: 400 ' + InputPending('f' * 32).message())
        if reason == 'missing-envelope': rejection = replace(rejection, result=receipt)
        if reason in {'ordinary400', 'ordinary409'}:
            rejection = replace(rejection, api_error_status=int(reason[-3:]), result='private permanent error')
        if reason == 'wrong-status': rejection = replace(rejection, api_error_status=409)
        if reason == 'stop': agent.interrupt()
        if reason == 'model-limit':
            agent.context.spec['max_iterations'] = agent.model_calls
            assert not agent.before_model(b'{"messages": []}')
        yield rejection
        await asyncio.Event().wait()  # Suppressing a terminal result would hang.

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream)
    result = asyncio.run(asyncio.wait_for(agent._run('Work', 'Moyai'), timeout=1))
    assert not result['completed'] and result['interrupted'] == (reason == 'stop')
    assert len(observed.queries) == 1 and observed.exits == [True] and not resumed
    assert agent.transport_attempt == 0 and 'private' not in json.dumps(result)
    assert bool(agent.recovery_rejections) == (reason != 'already-consumed')


@pytest.mark.parametrize('reason', ['model-limit', 'attempt-limit', 'uncertain-tool', 'context-required',
                                   'changed-failure', 'stop', 'boundary'])
def test_claude_terminal_recovery_entry_spends_no_attempt_or_status(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str) -> None:
    from sandbox.transport_recovery import MAX_TRANSPORT_ATTEMPTS
    from test_codex_sdk import install_transport_relay, model_failure
    agent, events = make_agent(monkeypatch, tmp_path)
    relay, resumed = install_transport_relay(agent)
    observed_failure = model_failure(relay)
    if reason == 'model-limit': agent.model_calls = agent.context.spec['max_iterations']
    if reason == 'attempt-limit': agent.transport_attempt = MAX_TRANSPORT_ATTEMPTS
    if reason == 'uncertain-tool': relay.uncertain_tool = True
    if reason == 'context-required': relay.context_required = {'reason': 'context handoff needed'}
    if reason == 'changed-failure': relay.last_failure = dict(observed_failure)
    if reason == 'stop': agent.interrupt()
    if reason == 'boundary': agent.boundary_failed = True
    initial = agent.transport_attempt

    class Client:
        async def query(self, prompt: str) -> None:
            pytest.fail('A terminal boundary cannot send another query')

    assert not asyncio.run(agent.recover_model(Client(), observed_failure))
    assert agent.transport_attempt == initial and events == [] and not resumed


@pytest.mark.parametrize('phase', ['initial', 'readiness', 'query'])
@pytest.mark.parametrize('failure', ['error_during_execution', 'error_max_budget_usd', 'http400'])
def test_claude_terminal_result_during_recovery_cannot_be_replaced_by_success(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, failure: str) -> None:
    from contextlib import closing
    from dataclasses import replace
    from sandbox.context_store import ContextStore
    from sandbox.transport_recovery import recovery_marker, valid_retry
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path, timeout=6)
    terminal = replace(recovery_result(),
        subtype='success' if failure == 'http400' else failure,
        api_error_status=400 if failure == 'http400' else None, result='private terminal error')

    async def stream():
        model_failure(relay, 'original-outage')['route'] = '/v1/messages'
        if phase != 'initial':
            yield recovery_result()
            while not agent.transport_attempt or (phase == 'query' and len(observed.queries) == 1):
                await asyncio.sleep(.005)
        yield terminal
        while len(observed.queries) == 1:
            await asyncio.sleep(.005)
        yield recovery_result(False)
        observed.release_query.set()

    with closing(ContextStore(tmp_path / 'context.sqlite3', 'terminal-recovery')) as store:
        store.initialize([])
        agent.context_store = store
        agent.next_maintenance = float('inf')
        relay, resumed, observed = recovery_client(monkeypatch, agent, stream, hold_continuation=True)
        result = asyncio.run(agent._run('Work', 'Moyai'))
        assert agent.journal.context_store is store and not store.pending and not agent.journal.pending
        assert store.checkpoint()['seq'] > 0
        if phase != 'query':
            assert valid_retry({'version': 1, 'failure': relay.last_failure, 'checkpoint': store.checkpoint()})
        assert recovery_marker(agent, result) is None, 'A terminal SDK result must also stop cold recovery'
    assert result['failed'] and not result['completed']
    assert agent.boundary_failed
    assert result['sdk_failure']['native_status'] == terminal.subtype
    if phase != 'query':
        assert result['sdk_failure']['broker_request_id'] == 'original-outage'
    if failure == 'http400':
        assert result['sdk_failure']['http_status'] == 400
    assert len(observed.queries) == (2 if phase == 'query' else 1)
    assert len(resumed) == (1 if phase == 'query' else 0) and observed.exits == [True]
    assert agent.transport_attempt == (0 if phase == 'initial' else 1)
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('failure_kind', ['http502', 'network', 'interrupted-stream'])
def test_claude_second_transport_failure_waits_for_the_pending_continuation_query(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_kind: str) -> None:
    from dataclasses import replace
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path, timeout=12)

    async def stream():
        model_failure(relay, 'first-outage')['route'] = '/v1/messages'
        yield recovery_result()
        while len(observed.queries) < 2:
            await asyncio.sleep(.005)
        assert relay.last_failure is None and not observed.release_query.is_set()
        failure = model_failure(relay, 'second-outage')
        failure['route'] = '/v1/messages'
        second_result = recovery_result()
        if failure_kind == 'network':
            failure['http_status'] = None  # The relay sends the SDK a local 502.
        if failure_kind == 'interrupted-stream':
            failure.update(http_status=200, response_started=True, transport_interrupted=True)
            second_result = replace(second_result, api_error_status=None)
        yield second_result
        observed.release_query.set()
        while len(observed.queries) < 3:
            await asyncio.sleep(.005)
        yield recovery_result(False)

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream, hold_continuation=True)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert result['completed'], result
    assert resumed == ['first-outage', 'second-outage'] and agent.transport_attempt == 2
    assert len(observed.queries) == agent.model_calls == 3 and observed.exits == [True]


@pytest.mark.parametrize('reason', ['unauthorized', 'forbidden', 'tool_failure', 'partial_stream',
    'uncertain_tool', 'stop', 'readiness_forbidden', 'readiness_invalid', 'replaced_failure',
    'model_limit', 'recovery_deadline', 'task_deadline'])
def test_claude_recovery_preserves_terminal_boundaries(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str) -> None:
    from urllib.error import HTTPError
    from sandbox.broker_failure import failure
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path,
        transport_recovery_seconds=.05 if reason == 'recovery_deadline' else 10,
        timeout=.2 if reason == 'task_deadline' else 10)
    probes = []

    async def stream():
        await agent.tool_hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': 'original-command'}}, 'original', {})
        diagnostic = model_failure(relay)
        diagnostic['route'] = '/v1/messages'
        if reason in {'unauthorized', 'forbidden', 'tool_failure'}:
            status = {'unauthorized': 401, 'forbidden': 403, 'tool_failure': 502}[reason]
            route = '/tools/call' if reason == 'tool_failure' else '/v1/messages'
            relay.last_failure = failure(route, 'blocked', HTTPError('http://broker', status, '', {}, None), status=status)
        if reason == 'partial_stream': diagnostic['response_started'] = True
        if reason == 'model_limit': agent.context.spec['max_iterations'] = 1
        if reason == 'task_deadline': await asyncio.sleep(.15)
        yield recovery_result()
        await asyncio.Event().wait()  # The surviving native client remains open.

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream)

    def ready(*, timeout: float) -> bool:
        probes.append(timeout)
        assert observed.exits == [] and observed.queries == ['Work']
        if reason == 'stop': agent.interrupt()
        if reason == 'uncertain_tool': relay.uncertain_tool = True
        if reason == 'replaced_failure': relay.last_failure = dict(relay.last_failure)
        if reason == 'readiness_forbidden': raise HTTPError('http://broker', 403, '', {}, None)
        if reason == 'readiness_invalid': raise ValueError('private invalid readiness response')
        return True

    relay.model_ready = ready
    started = time.monotonic()
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert not result['completed'] and not resumed and observed.queries == ['Work']
    assert observed.exits == [True] and agent.journal.pending == {'original'}
    assert relay.model_failed and result['interrupted'] == (reason == 'stop')
    assert bool(probes) == (reason in {'stop', 'uncertain_tool', 'replaced_failure',
                                      'readiness_forbidden', 'readiness_invalid'})
    if reason in {'readiness_forbidden', 'readiness_invalid', 'recovery_deadline'}:
        assert agent.boundary_failed
    if reason == 'task_deadline':
        assert result['sdk_failure']['exception_type'] == 'TimeoutError'
        assert time.monotonic() - started < .4, 'Recovery must retain the original deadline'
    assert 'private' not in json.dumps(result)


def test_claude_transport_budget_survives_invocation_reentry(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from sandbox.transport_recovery import MAX_TRANSPORT_ATTEMPTS
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path, transport_attempt=MAX_TRANSPORT_ATTEMPTS - 1)
    invocations = []

    async def stream():
        invocations.append(True)
        model_failure(relay, 'failure-' + str(len(invocations)))['route'] = '/v1/messages'
        before = len(observed.queries)
        yield recovery_result()
        while len(observed.queries) == before:
            await asyncio.sleep(.005)
        yield recovery_result(False)

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream)
    first = asyncio.run(agent._run('Work', 'Moyai'))
    second = asyncio.run(agent._run('Continue after context handoff', 'Moyai'))
    assert first['completed'] and second['failed']
    assert resumed == ['failure-1'] and agent.transport_attempt == MAX_TRANSPORT_ATTEMPTS
    assert len(observed.queries) == 3 and observed.exits == [True, True]


@pytest.mark.parametrize('cancel', [False, True], ids=['steering', 'cancellation'])
def test_claude_recovery_wait_keeps_input_and_cancellation_live(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel: bool) -> None:
    from claude_agent_sdk import SystemMessage, UserMessage
    from test_codex_sdk import model_failure
    agent, _ = make_agent(monkeypatch, tmp_path)
    probe_started, release_probe = threading.Event(), threading.Event()

    async def stream():
        tool = {'tool_name': 'Bash', 'tool_input': {'command': 'original-command'}}
        await agent.tool_hook({**tool, 'hook_event_name': 'PreToolUse'}, 'original', {})
        model_failure(relay)['route'] = '/v1/messages'
        yield recovery_result()
        # More than the SDK's 100-message queue can hold, before a late hook.
        # Readiness waiting must leave the receiver able to drain this backlog.
        for index in range(120):
            yield SystemMessage(subtype='task_progress', data={'index': index})
        await agent.tool_hook({**tool, 'hook_event_name': 'PostToolUse', 'tool_response': 'Original receipt'}, 'original', {})
        while len(observed.queries) == 1:
            await asyncio.sleep(.005)
        # The first resumed inference must already see corrections accepted
        # during the outage; a later native query can act on stale instructions.
        assert 'Keep the original preview running' in observed.queries[1]
        for envelope in observed.envelopes:
            yield UserMessage(content=envelope['message']['content'], uuid=envelope['uuid'])
        yield recovery_result(False)

    relay, resumed, observed = recovery_client(monkeypatch, agent, stream)

    def ready(*, timeout: float) -> bool:
        assert agent.journal.completed_tools == 1 and not agent.journal.pending
        probe_started.set()
        assert release_probe.wait(timeout=1)
        return True

    relay.model_ready = ready

    async def run():
        task = asyncio.create_task(agent._run('Work', 'Moyai'))
        try:
            async with asyncio.timeout(3):
                while not probe_started.is_set():
                    await asyncio.sleep(.005)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not resumed and observed.queries == ['Work']
            else:
                assert agent.accept_input({'id': 91, 'content': 'Keep the original preview running'})
                release_probe.set()
                result = await task
                assert result['completed'], result
                assert len(observed.queries) == 2 and len(resumed) == 1
                assert sum('Keep the original preview' in str(m.get('content')) for m in result['messages']) == 1
            assert observed.exits == [True]
        finally:
            release_probe.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
