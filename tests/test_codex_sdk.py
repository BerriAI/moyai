import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import codex_harness
from sandbox.activity import ActivityReporter
from sandbox.context_store import ContextStore
from sandbox.harness_agent import TurnJournal


@pytest.fixture
def codex_agent(monkeypatch, tmp_path):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'run-capability')
    events = []
    activity = ActivityReporter(lambda *args: events.append(args), tracing=True)
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    agent = codex_harness.CodexAgent(
        spec={'model': 'openai/gpt-6-astra', 'max_iterations': 6},
        relay=SimpleNamespace(url='http://127.0.0.1:1234'),
        config={'mcp_servers': {'workspace': {'command': 'python', 'args': ['mcp_bridge.py']}}},
        activity=activity, step=lambda: None, cwd=str(tmp_path), definition=None,
        context_store=store)
    agent.journal = TurnJournal([], 'Inspect the workspace.', store)
    yield agent, events, store
    agent.close()
    store.close()


def sdk_event(method, body):
    return SimpleNamespace(method=method, payload=SimpleNamespace(model_dump=lambda **kwargs: body))


def install_codex_client(monkeypatch, agent, stream, late_items=()):
    observed = SimpleNamespace(homes=[], prompts=[], thread_options=[], steers=[], steer_error=None)

    class Client:
        def __init__(self, config):
            home = Path(config.env['CODEX_HOME'])
            assert home.is_dir()
            (home / 'native-private-transcript').write_text('requester-native-secret')
            observed.homes.append(home)
            self.notifications = stream()
            self.turn_count = 0
            self.late_items = iter(late_items)

        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass

        async def thread_start(self, options):
            observed.thread_options.append(options)
            return SimpleNamespace(thread=SimpleNamespace(id='fresh-thread'))

        async def turn_start(self, thread_id, prompt):
            assert thread_id == 'fresh-thread'
            observed.prompts.append(prompt)
            self.turn_count += 1
            return SimpleNamespace(turn=SimpleNamespace(id=f'fresh-turn-{self.turn_count}'))

        async def turn_steer(self, thread_id, expected_turn_id, input_items):
            observed.steers.append((thread_id, expected_turn_id, input_items))
            if observed.steer_error:
                raise observed.steer_error
            return SimpleNamespace(turn_id=expected_turn_id)

        async def next_turn_notification(self, turn_id):
            if turn_id != f'fresh-turn-{self.turn_count}':
                from openai_codex.errors import TransportClosedError
                try:
                    return next(self.late_items)
                except StopIteration:
                    raise TransportClosedError('Turn is no longer streaming') from None
            return await anext(self.notifications)

    monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', Client)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    return observed


@pytest.mark.parametrize('delivery', ['active', 'ended', 'accepted-at-finish', 'unknown-error'])
@pytest.mark.parametrize('phase', ['final_answer', None])
def test_live_input_uses_current_native_turn_and_preserves_journal(codex_agent, monkeypatch, delivery, phase):
    from openai_codex.errors import InvalidRequestError, TransportClosedError
    agent, events, store = codex_agent

    async def stream():
        assert agent.accept_input({'id': 73, 'content': 'Also verify the correction.'})
        for _ in range(40):
            if observed.steers:
                break
            await asyncio.sleep(0.01)
        assert observed.steers, 'Live input must reach the native SDK before its result'
        yield sdk_event('item/completed', {'item': {'id': 'commentary', 'type': 'agentMessage',
            'phase': 'commentary', 'text': 'Earlier progress.'}})
        yield sdk_event('item/completed', {'item': {'id': 'original', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Old answer.'}})
        if delivery == 'ended':
            yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
        yield sdk_event('item/completed', {'item': {'id': 'guidance', 'type': 'userMessage'}})
        if delivery == 'accepted-at-finish':
            # Native completion drains a late accepted input into history even
            # when the final pending-input check has already ended sampling.
            yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
            assert len(observed.prompts) == 2
        yield sdk_event('item/completed', {'item': {'id': 'corrected', 'type': 'agentMessage',
            'phase': phase, 'text': 'Corrected answer.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    if delivery == 'ended':
        observed.steer_error = InvalidRequestError(-32600, 'no active turn to steer')
    elif delivery == 'unknown-error':
        observed.steer_error = TransportClosedError('Connection lost after write')
    result = agent.run_conversation('Continue.', conversation_history=[], system_message='Moyai')
    assert observed.steers == [('fresh-thread', 'fresh-turn-1',
                               '[User correction to the current task]\nAlso verify the correction.')]
    assert len(observed.prompts) == (2 if delivery in {'ended', 'accepted-at-finish'} else 1)
    if delivery == 'accepted-at-finish':
        assert 'Also verify the correction.' not in observed.prompts[1]
    assert result['completed'] is (delivery != 'unknown-error')
    if result['completed']:
        assert result['final_response'] == 'Corrected answer.'
    assert len([message for message in agent.journal.messages
                if message.get('content') == observed.steers[0][2]]) == 1
    assert not agent.accept_input({'id': 74, 'content': 'Too late'})
    assert not agent.stopped.is_set()


def test_correction_racing_finalization_continues_same_sdk_session(codex_agent, monkeypatch):
    agent, events, store = codex_agent

    async def stream():
        close = agent.inputs.close_if_empty
        def racing_close():
            agent.inputs.close_if_empty = close
            assert agent.accept_input({'id': 81, 'content': 'Check the final race.'})
            return close()
        agent.inputs.close_if_empty = racing_close
        yield sdk_event('item/completed', {'item': {'id': 'old', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Premature answer.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
        assert len(observed.prompts) == 2
        yield sdk_event('item/completed', {'item': {'id': 'new', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Final race incorporated.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    result = agent.run_conversation('Continue.', conversation_history=[], system_message='Moyai')
    assert result['completed'] and result['final_response'] == 'Final race incorporated.'
    assert len(observed.homes) == 1 and not observed.steers
    assert observed.prompts[1].endswith('Check the final race.')


def assistant_prose(messages):
    return [message['content'] for message in messages
            if message['role'] == 'assistant' and not message.get('tool_calls')]


def saved_prose(store):
    return assistant_prose(json.loads(row[0]) for row in store.db.execute(
        'SELECT message FROM journal ORDER BY seq'))


def assert_public_messages(result, events, store, *, saved, commentary, response):
    assert saved_prose(store) == saved
    restored = ContextStore(store.path, 'run')
    try:
        assert saved_prose(restored) == saved
        assert restored.history() == store.history()
    finally:
        restored.close()
    assert [event[1] for event in events if event[0] == 'message'] == commentary
    assert result['final_response'] == response


@pytest.mark.parametrize('items,saved,commentary,response', [
    pytest.param([('a', 'commentary', 'First.'), ('b', None, 'Second.')],
                 ['First.', 'Second.'], ['First.', 'Second.'], 'First.\nSecond.', id='commentary-fallback'),
    pytest.param([('a', 'final_answer', 'Answer.')],
                 ['Answer.'], [], 'Answer.', id='final-only'),
    pytest.param([('a', 'commentary', 'Update.'), ('b', 'final_answer', 'Answer.')],
                 ['Update.', 'Answer.'], ['Update.'], 'Answer.', id='commentary-and-final'),
    pytest.param([('a', 'commentary', 'Same.'), ('b', 'final_answer', 'Same.')],
                 ['Same.', 'Same.'], ['Same.'], 'Same.', id='equal-text-distinct-phases'),
    pytest.param([('a', 'commentary', 'Same.'), ('b', 'commentary', 'Same.')],
                 ['Same.', 'Same.'], ['Same.', 'Same.'], 'Same.\nSame.', id='equal-text-distinct-ids'),
    pytest.param([('a', 'commentary', 'Update.'), ('a', 'commentary', 'Update.')],
                 ['Update.'], ['Update.'], 'Update.', id='duplicate-commentary-id'),
    pytest.param([('a', 'final_answer', 'Answer.'), ('a', 'final_answer', 'Answer.')],
                 ['Answer.'], [], 'Answer.', id='duplicate-final-id'),
    pytest.param([('a', 'final_answer', 'First.'), ('b', 'final_answer', 'Second.')],
                 ['First.', 'Second.'], [], 'First.\nSecond.', id='multiple-final-items'),
    pytest.param([('a', 'final_answer', 'Same.'), ('b', 'final_answer', 'Same.')],
                 ['Same.', 'Same.'], [], 'Same.\nSame.', id='equal-text-distinct-final-ids'),
    pytest.param([('a', 'commentary', ''), ('b', 'final_answer', '')],
                 [], [], '', id='empty-items'),
])
def test_completed_native_messages_project_once(codex_agent, monkeypatch, items, saved, commentary, response):
    agent, events, store = codex_agent
    before_settle = []

    async def stream():
        for item_id, phase, text in items:
            item = {'id': item_id, 'type': 'agentMessage', 'text': text}
            if phase is not None:
                item['phase'] = phase
            yield sdk_event('item/started', {'item': {**item, 'text': 'Unconfirmed start.'}})
            yield sdk_event('item/completed', {'item': item})
        before_settle.append(saved_prose(store))
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    install_codex_client(monkeypatch, agent, stream)
    result = agent.run_conversation('Continue.', conversation_history=[], system_message='Moyai')
    assert result['completed'] and not result['failed'] and not result['interrupted']
    assert before_settle == [commentary]
    assert assistant_prose(result['messages']) == saved
    assert_public_messages(result, events, store, saved=saved, commentary=commentary, response=response)


@pytest.mark.parametrize('outcome', ['failed-status', 'provider-error', 'interrupted', 'pending-tool', 'boundary-failed'])
def test_unsettled_turn_discards_final_candidates_but_keeps_commentary(codex_agent, monkeypatch, outcome):
    agent, events, store = codex_agent

    async def stream():
        for item_id, phase, text in [('a', 'commentary', 'Saved update.'), ('b', 'final_answer', 'Unconfirmed final.')]:
            yield sdk_event('item/completed', {'item': {
                'id': item_id, 'type': 'agentMessage', 'phase': phase, 'text': text}})
        if outcome == 'provider-error':
            raise RuntimeError('private-provider-error')
        if outcome == 'interrupted':
            agent.interrupt()
        if outcome == 'pending-tool':
            yield sdk_event('item/started', {'item': native_item('commandExecution')[0]})
        if outcome == 'boundary-failed':
            agent.boundary_failed = True
        yield sdk_event('turn/completed', {'turn': {
            'status': 'failed' if outcome == 'failed-status' else 'completed'}})
        if outcome == 'pending-tool':
            yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    install_codex_client(monkeypatch, agent, stream)
    result = agent.run_conversation('Continue.', conversation_history=[], system_message='Moyai')
    assert not result['completed']
    assert result['interrupted'] == (outcome == 'interrupted')
    assert result['failed'] == (outcome != 'interrupted')
    assert assistant_prose(result['messages']) == ['Saved update.']
    assert_public_messages(result, events, store, saved=['Saved update.'], commentary=['Saved update.'],
        response={
            'failed-status': 'Codex stopped (failed). Saved tool receipts are preserved.',
            'provider-error': 'Codex stopped (RuntimeError). Saved tool receipts are preserved.',
            'interrupted': 'Codex stopped before completing the response. Saved tool receipts are preserved.',
            'pending-tool': 'Codex stopped (incomplete turn, 1 unresolved tool(s)). Saved tool receipts are preserved.',
            'boundary-failed': 'Codex stopped (incomplete turn). Saved tool receipts are preserved.',
        }[outcome])
    assert bool(store.pending) == (outcome == 'pending-tool')


@pytest.mark.parametrize('outcome', ['complete', 'stop', 'boundary', 'failed', 'limit', 'timeout'])
def test_settlement_uses_original_receipts_and_budget_before_publishing(codex_agent, monkeypatch, outcome):
    agent, events, store = codex_agent
    started, finished = native_item('commandExecution')
    namespaces = []
    if outcome == 'timeout': agent.context.spec['timeout'] = 0.05

    async def stream():
        assert agent.before_model()
        namespaces.append(agent.journal.call_namespace)
        yield sdk_event('item/started', {'item': started})
        yield sdk_event('item/completed', {'item': {
            'id': 'old', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Premature answer.'}})
        if outcome == 'stop': agent.interrupt()
        if outcome == 'boundary': agent.boundary_failed = True
        if outcome == 'limit': agent.context.spec['max_iterations'] = 1
        yield sdk_event('turn/completed', {'turn': {'status': 'failed' if outcome == 'failed' else 'completed'}})
        if outcome == 'timeout': await asyncio.Event().wait()
        if outcome == 'limit':
            assert not agent.before_model()
            yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})
            return
        assert agent.before_model()
        namespaces.append(agent.journal.call_namespace)
        yield sdk_event('item/completed', {'item': {
            'id': 'new', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Confirmed answer.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream,
        late_items=[sdk_event('item/completed', {'item': finished})] if outcome == 'complete' else [])
    result = agent.run_conversation('Work', conversation_history=[], system_message='Moyai')
    assert len(observed.prompts) == (2 if outcome in {'complete', 'limit', 'timeout'} else 1)
    assert result['completed'] == (outcome == 'complete')
    assert result['interrupted'] == (outcome == 'stop')
    assert saved_prose(store) == (['Confirmed answer.'] if outcome == 'complete' else [])
    assert 'Premature answer.' not in json.dumps(result)
    assert bool(store.pending) == (outcome != 'complete')
    assert len(set(namespaces)) == 1
    assert agent.model_calls == (2 if outcome == 'complete' else 1)
    if outcome == 'complete':
        assert sum(message.get('role') == 'tool' for message in result['messages']) == 1
        assert result['final_response'] == 'Confirmed answer.'
    if outcome == 'limit':
        assert result['sdk_failure']['boundary_reason'] == 'model call limit reached'
    if outcome == 'timeout':
        assert result['sdk_failure']['exception_type'] == 'TimeoutError'


@pytest.mark.parametrize('outcome', ['receipt', 'unknown', 'stop', 'deadline'])
def test_context_receipt_drain_preserves_unknowns_and_respects_stop(codex_agent, monkeypatch, outcome):
    from openai_codex.errors import TransportClosedError
    agent, events, store = codex_agent
    started, finished = native_item('commandExecution')
    agent.record_item(started, completed=False)
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.15)
    reads = []

    class Client:
        async def next_turn_notification(self, turn_id):
            reads.append(turn_id)
            if len(reads) == 1:
                # Even an output for this exact ID is not a durable receipt.
                return sdk_event('rawResponseItem/completed', {'item': {
                    'type': 'custom_tool_call_output', 'call_id': started['id'],
                    'output': 'raw-private-marker'}})
            if outcome == 'stop':
                agent.interrupt()
            if outcome == 'receipt' and turn_id == 'original':
                return sdk_event('item/completed', {'item': finished})
            raise TransportClosedError('Turn is no longer streaming')

    async def drain():
        if outcome == 'deadline':
            # The enclosing task deadline wins over the receipt grace period.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await agent.settle_context_receipts(Client(), ['settlement', 'original'])
        else:
            await agent.settle_context_receipts(Client(), ['settlement', 'original'])

    asyncio.run(drain())
    assert bool(agent.journal.pending) == bool(store.pending) == (outcome != 'receipt')
    assert agent.journal.completed_tools == int(outcome == 'receipt')
    assert agent.model_calls == 0
    assert 'raw-private-marker' not in json.dumps([agent.journal.messages, events, store.history()])


def test_native_restart_accepts_reused_message_ids(codex_agent, monkeypatch):
    agent, events, store = codex_agent
    invocation = 0

    async def stream():
        nonlocal invocation
        invocation += 1
        for item_id, phase, text in [('a', 'commentary', 'Update.'), ('b', 'final_answer', f'Answer {invocation}.')]:
            yield sdk_event('item/completed', {'item': {
                'id': item_id, 'type': 'agentMessage', 'phase': phase, 'text': text}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    install_codex_client(monkeypatch, agent, stream)
    # A context-recovery invocation keeps the outer TurnJournal, but the native
    # item IDs belong to the new invocation even if their text also matches.
    results = [asyncio.run(agent._run('Continue.', 'Moyai')) for _ in range(2)]
    assert all(result['completed'] for result in results)
    expected = ['Update.', 'Answer 1.', 'Update.', 'Answer 2.']
    assert assistant_prose(results[-1]['messages']) == expected
    assert_public_messages(results[-1], events, store, saved=expected,
                           commentary=['Update.', 'Update.'], response='Answer 2.')


def native_item(kind, *, failed=False):
    item = {'id': 'native-call', 'type': kind, 'status': 'inProgress'}
    if kind == 'commandExecution':
        item.update(command='printf proof', cwd='/workspace', commandActions=[])
        finished = {**item, 'status': 'completed', 'aggregatedOutput': 'proof',
                    'exitCode': 1 if failed else 0}
    elif kind == 'fileChange':
        item['changes'] = [{'path': '/workspace/proof.txt', 'kind': {'type': 'add'}, 'diff': '+proof'}]
        finished = {**item, 'status': 'failed' if failed else 'completed'}
    else:
        item.update(server='moyai', tool='tool_call', arguments={'name': 'repository_read', 'arguments': {}})
        finished = {**item, 'status': 'completed', 'result': {
            'content': [{'type': 'text', 'text': 'proof'}], 'isError': failed}}
    return item, finished


@pytest.mark.parametrize('kind', ['commandExecution', 'fileChange', 'mcpToolCall'])
@pytest.mark.parametrize('failed', [False, True])
def test_native_tool_receipts_are_paired_once_and_keep_failure(codex_agent, kind, failed):
    agent, events, store = codex_agent
    started, finished = native_item(kind, failed=failed)
    agent.record_item(started, completed=False)
    agent.record_item(started, completed=False)
    assert agent.journal.pending == {'native-call'}
    agent.record_item(finished, completed=True)
    agent.record_item(finished, completed=True)
    assert not agent.journal.pending and not store.pending
    calls = [message for message in agent.journal.messages if message.get('tool_calls')]
    receipts = [message for message in agent.journal.messages if message.get('role') == 'tool']
    assert len(calls) == len(receipts) == 1
    assert calls[0]['tool_calls'][0]['id'] == receipts[0]['tool_call_id']
    tool_events = [event[2] for event in events if event[0] == 'tool']
    assert [event['phase'] for event in tool_events] == ['started', 'error' if failed else 'completed']


@pytest.mark.parametrize('omit', [False, True])
def test_private_mcp_tool_names_preserve_existing_redaction(codex_agent, omit):
    agent, events, store = codex_agent
    agent.context.activity.omit_private_tool_payloads = omit
    item = {'id': 'memory-call', 'type': 'mcpToolCall', 'server': 'moyai', 'tool': 'memory_save',
            'arguments': {'content': 'requester-private-marker'}, 'status': 'inProgress'}
    agent.record_item(item, completed=False)
    agent.record_item({**item, 'status': 'completed', 'result': {
        'content': [{'type': 'text', 'text': '{"saved":true}'}]}}, completed=True)
    saved = ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
    assert 'requester-private-marker' not in saved
    public = [event for event in events if event[0] != 'trace']
    traces = [event for event in events if event[0] == 'trace']
    assert 'requester-private-marker' not in json.dumps(public)
    assert ('requester-private-marker' in json.dumps(traces)) is (not omit)
    assert not store.pending


@pytest.mark.parametrize('output_type', ['function_call_output', 'custom_tool_call_output'])
@pytest.mark.parametrize('started_before_request', [False, True])
def test_checkpoint_waits_for_native_receipt_instead_of_copying_wire_output(codex_agent, output_type, started_before_request):
    agent, _, store = codex_agent
    started, finished = native_item('commandExecution')
    if started_before_request:
        agent.record_item(started, completed=False)
    stepped = threading.Event()
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
                                           agent.context.activity, stepped.set, agent.context.cwd)
    # This output is not a receipt source. Only item/completed can settle the call.
    request = json.dumps({'input': [{'type': output_type, 'call_id': 'native-call',
                                    'output': 'wire-private-marker'}]}).encode()
    entering = threading.Event()
    def before_model():
        entering.set()
        return agent.before_model(request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(before_model)
        assert entering.wait(1)
        assert not stepped.wait(0.05)
        assert not future.done()
        if not started_before_request:
            assert not store.pending
            agent.record_item(started, completed=False)
            assert not stepped.wait(0.05) and not future.done()
        assert store.pending
        agent.record_item(finished, completed=True)
        assert future.result(timeout=2) is True
    assert stepped.is_set()
    assert 'wire-private-marker' not in json.dumps(agent.journal.messages)


def test_unknown_native_outcome_blocks_checkpoint_without_creating_receipt(codex_agent, monkeypatch):
    agent, _, store = codex_agent
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.01)
    stepped = []
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
                                           agent.context.activity, lambda: stepped.append(True), agent.context.cwd)
    request = json.dumps({'input': [{'type': 'function_call_output', 'call_id': 'unobserved',
                                    'output': 'untrusted-wire-output'}]}).encode()
    before = list(agent.journal.messages)
    try:
        allowed = agent.before_model(request)
    except RuntimeError:
        allowed = False
    assert allowed is False and not stepped
    assert agent.journal.messages == before and not store.pending


def test_cooperative_stop_happens_only_after_parallel_tool_receipts(codex_agent):
    agent, _, _ = codex_agent
    started, finished = native_item('commandExecution')
    agent.record_item(started, completed=False)
    agent.record_item({**started, 'id': 'second'}, completed=False)
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
                                           agent.context.activity, agent.interrupt, agent.context.cwd)
    agent.record_item(finished, completed=True)
    assert agent.before_model() is True
    assert not agent.stopped.is_set()
    agent.record_item({**finished, 'id': 'second'}, completed=True)
    assert agent.before_model() is False
    assert agent.stopped.is_set()


@pytest.mark.parametrize('kind', ['reasoning', 'contextCompaction'])
def test_private_runtime_items_never_become_public_history(codex_agent, kind):
    agent, events, store = codex_agent
    before = list(agent.journal.messages)
    agent.record_item({'id': 'private', 'type': kind, 'text': 'private-runtime-marker',
                       'summary': ['private-runtime-marker']}, completed=True)
    assert agent.journal.messages == before and not events
    assert 'private-runtime-marker' not in ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))


def test_sdk_config_clears_inherited_credentials_and_uses_only_broker(codex_agent, monkeypatch, tmp_path):
    agent, _, _ = codex_agent
    inherited = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY',
                 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE')
    for key in (*inherited, 'CODEX_HOME'):
        monkeypatch.setenv(key, 'inherited-private-marker')
    project = Path(agent.context.cwd) / '.codex'
    project.mkdir()
    (project / 'config.toml').write_text('[mcp_servers.foreign]\ncommand="untrusted-command"\n')
    home = tmp_path / 'fresh-home'
    config = agent.sdk_config(home)
    settings = tomllib.loads('\n'.join(config.config_overrides))
    assert config.cwd == str(home) and config.env['CODEX_HOME'] == str(home)
    assert config.env['WORKSPACE_RUN_TOKEN'] == 'run-capability'
    assert 'inherited-private-marker' not in json.dumps(config.env)
    assert settings['model_provider'] == 'moyai'
    provider = settings['model_providers']['moyai']
    assert (provider['base_url'], provider['env_key'], provider['wire_api']) == (
        agent.context.relay.url + '/v1', 'WORKSPACE_RUN_TOKEN', 'responses')
    assert provider['request_max_retries'] == provider['stream_max_retries'] == 0
    assert not provider['requires_openai_auth'] and not provider['supports_websockets']
    assert set(settings['mcp_servers']) == {'moyai'}
    assert settings['projects'][str(Path(agent.context.cwd).resolve())]['trust_level'] == 'untrusted'
    assert settings['history']['persistence'] == 'none'
    assert settings['cli_auth_credentials_store'] == 'ephemeral'
    # Exercise the real launch prefix: an empty native override is not unset.
    from codex_cli_bin import bundled_codex_path
    launch = list(config.launch_args_override)
    prefix = launch[:launch.index(str(bundled_codex_path()))]
    probe = 'import json,os; print(json.dumps({key: key in os.environ for key in ' + repr(inherited) + '}))'
    process = subprocess.run([*prefix, sys.executable, '-c', probe], env={**os.environ, **config.env},
                             capture_output=True, text=True, check=True, timeout=5)
    assert not any(json.loads(process.stdout).values())


@pytest.mark.parametrize('fail_after_tool', [False, True])
def test_each_invocation_uses_disposable_native_state_and_preserves_public_receipts(codex_agent, monkeypatch, fail_after_tool):
    agent, events, store = codex_agent
    started, finished = native_item('commandExecution')
    async def stream():
        yield sdk_event('item/started', {'item': started})
        yield sdk_event('item/completed', {'item': finished})
        if fail_after_tool:
            raise RuntimeError('private-provider-error')
        yield sdk_event('item/completed', {'item': {
            'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Public answer.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
    observed = install_codex_client(monkeypatch, agent, stream)
    results = [agent.run_conversation('Continue the task.', conversation_history=[], system_message='Moyai')
               for _ in range(2)]
    assert len(set(observed.homes)) == 2 and all(not home.exists() for home in observed.homes)
    assert all(options['ephemeral'] and options['modelProvider'] == 'moyai'
               and options['experimentalRawEvents'] for options in observed.thread_options)
    assert all(result['completed'] == (not fail_after_tool) and result['failed'] == fail_after_tool for result in results)
    assert all('requester-native-secret' not in prompt for prompt in observed.prompts)
    assert 'proof' in observed.prompts[1] and not store.pending
    saved = ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
    assert 'private-provider-error' not in json.dumps([results, events])
    assert 'requester-native-secret' not in saved
    assert sum(message.get('role') == 'tool' for message in results[-1]['messages']) == 1
    assert saved_prose(store) == ([] if fail_after_tool else ['Public answer.', 'Public answer.'])


@pytest.mark.parametrize('outer_marker_first', [False, True])
@pytest.mark.parametrize('output_id', ['outer-code-call', 'native-call'])
def test_code_mode_output_allows_polling_but_lifecycle_requires_nested_receipt(
        codex_agent, monkeypatch, outer_marker_first, output_id):
    agent, _, _ = codex_agent
    notifications, processed = queue.Queue(), queue.Queue()
    stepped = threading.Event()
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
                                           agent.context.activity, stepped.set, agent.context.cwd)
    started, finished = native_item('commandExecution')
    def event(method, body):
        payload = (SimpleNamespace(params=body) if method == 'rawResponseItem/completed' else
                   SimpleNamespace(model_dump=lambda **kwargs: body))
        return SimpleNamespace(method=method, payload=payload)
    terminal = event('turn/completed', {'turn': {'status': 'completed'}})
    class Client:
        def __init__(self, config): self.previous = 'ready'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def thread_start(self, options):
            return SimpleNamespace(thread=SimpleNamespace(id='thread'))
        async def turn_start(self, thread_id, prompt):
            return SimpleNamespace(turn=SimpleNamespace(id='turn'))
        async def next_turn_notification(self, turn_id):
            processed.put(self.previous)
            notification = await asyncio.to_thread(notifications.get, True, 2)
            self.previous = notification.method
            return notification
    monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', Client)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    request = json.dumps({'input': [{'type': 'custom_tool_call_output', 'call_id': output_id,
                                    'output': 'wire-private-marker'}]}).encode()
    nested = event('item/completed', {'item': finished})
    outer = event('rawResponseItem/completed', {'item': {
        'type': 'custom_tool_call_output', 'call_id': output_id, 'output': 'raw-private-marker'}})
    with ThreadPoolExecutor(max_workers=2) as executor:
        run = executor.submit(agent.run_conversation, 'Do the task.', conversation_history=[], system_message='Moyai')
        try:
            assert processed.get(timeout=2) == 'ready'
            notifications.put(event('item/started', {'item': started}))
            assert processed.get(timeout=2) == 'item/started'
            boundary = executor.submit(agent.before_model, request)
            assert not stepped.wait(0.05)
            first, second = (outer, nested) if outer_marker_first else (nested, outer)
            notifications.put(first)
            assert processed.get(timeout=2) == first.method
            if outer_marker_first:
                assert not stepped.wait(0.05)
                assert boundary.result(timeout=2) is True
                assert agent.journal.pending and agent.model_calls == 1
            elif output_id != 'native-call':
                assert not stepped.wait(0.05)
                assert not boundary.done()
            notifications.put(second)
            assert processed.get(timeout=2) == second.method
            assert boundary.result(timeout=2) is True
            if outer_marker_first:
                assert agent.before_model(request) is True
            assert stepped.is_set()
            notifications.put(terminal)
            result = run.result(timeout=2)
        finally:
            notifications.put(terminal)
    assert result['completed']
    receipts = [message for message in result['messages'] if message.get('role') == 'tool']
    assert len(receipts) == 1 and receipts[0]['tool_call_id'] == 'native-call'
    assert 'private-marker' not in json.dumps(result)


@pytest.mark.parametrize('native_completion', [False, True])
def test_raw_output_before_native_start_preserves_receipt_lifecycle(codex_agent, monkeypatch, native_completion):
    agent, events, store = codex_agent
    started, finished = native_item('mcpToolCall', failed=True)

    async def stream():
        # A fresh invocation must not inherit output IDs from its predecessor.
        assert not getattr(agent, 'observed_outputs', set())
        yield sdk_event('rawResponseItem/completed', {'item': {
            'type': 'function_call_output', 'call_id': started['id'], 'output': 'raw-private-marker'}})
        yield sdk_event('item/started', {'item': started})
        assert agent.journal.pending == {started['id']} and store.pending
        if native_completion:
            for _ in range(2):
                yield sdk_event('item/completed', {'item': finished})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
        if not native_completion:
            yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    install_codex_client(monkeypatch, agent, stream)
    results = [asyncio.run(agent._run('Continue.', 'Moyai')) for _ in range(2 if native_completion else 1)]
    assert all(result['completed'] is native_completion for result in results)
    assert bool(store.pending) is (not native_completion)
    receipts = [message for message in results[-1]['messages'] if message.get('role') == 'tool']
    assert len(receipts) == (2 if native_completion else 0)
    saved = [json.loads(row[0]) for row in store.db.execute('SELECT message FROM journal')]
    saved_receipts = [message for message in saved if message.get('role') == 'tool']
    assert len({receipt['tool_call_id'] for receipt in saved_receipts}) == len(receipts)
    tool_events = [event[2]['phase'] for event in events if event[0] == 'tool']
    assert tool_events == (['started', 'error'] * 2 if native_completion else ['started'])
    assert 'raw-private-marker' not in json.dumps([results, events, store.history()])
    if not native_completion:
        assert results[-1]['sdk_failure']['source'] == 'incomplete_turn'
        assert results[-1]['sdk_failure']['pending_tools'] == 1


def test_polling_counts_toward_limit_without_checkpointing_live_tools(codex_agent, monkeypatch):
    from sandbox.transport_recovery import recovery_marker
    agent, _, store = codex_agent
    agent.context.spec['max_iterations'] = 2
    maintenance = []
    monkeypatch.setattr(codex_harness, 'maintain_context', lambda *_: maintenance.append(True))
    agent.record_item(native_item('commandExecution')[0], completed=False)
    agent.observed_outputs.add('outer')
    agent.context = agent.context.__class__(agent.context.spec, agent.context.relay, agent.context.config,
        agent.context.activity, lambda: pytest.fail('Cannot checkpoint a running tool'), agent.context.cwd)
    request = json.dumps({'input': [{'type': 'custom_tool_call_output', 'call_id': 'outer'}]})
    assert agent.before_model(request) and agent.before_model(request)
    assert not agent.before_model(request)
    assert agent.model_calls == 2 and not maintenance
    assert agent.boundary_reason == 'model call limit reached'
    assert agent.journal.pending and store.pending
    assert recovery_marker(agent, {'failed': True}) is None


@pytest.mark.parametrize('body', [None, [], {'input': None},
    {'input': [{'type': 'custom_tool_call_output', 'call_id': []}]},
    {'input': [{'type': 'function_call_output'}]}])
def test_invalid_model_input_fails_closed_with_a_reason(codex_agent, body):
    agent, _, _ = codex_agent
    assert not agent.before_model(json.dumps(body))
    assert agent.boundary_failed and agent.boundary_reason == 'invalid model request'
    assert agent.model_calls == 0


def test_stop_wakes_a_missing_output_notification_wait(codex_agent):
    agent, _, _ = codex_agent
    request = json.dumps({'input': [{'type': 'function_call_output', 'call_id': 'missing'}]})
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(agent.before_model, request)
        agent.interrupt()
        assert pending.result(timeout=1) is False
    assert not agent.boundary_failed and agent.model_calls == 0


@pytest.mark.parametrize('installed', [None, '0.1.0', '0.161.0'])
def test_snapshot_repair_installs_only_pinned_codex_sdk(monkeypatch, installed):
    from importlib.metadata import PackageNotFoundError
    from sandbox import harness_dependencies
    calls = []
    def version(package):
        assert package == 'openai-codex'
        if installed is None:
            raise PackageNotFoundError(package)
        return installed
    monkeypatch.setattr('importlib.metadata.version', version)
    monkeypatch.setattr(harness_dependencies, 'ensure_pip', lambda: None)
    monkeypatch.setattr(harness_dependencies.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    harness_dependencies.prepare_codex()
    assert len(calls) == (installed != '0.161.0')
    if calls:
        assert calls[0][1:] == ['-m', 'pip', 'install', 'openai-codex==0.161.0']


@pytest.mark.parametrize('source', ['notification', 'turn', 'exception'])
def test_codex_persists_safe_native_failure_metadata(codex_agent, monkeypatch, source):
    agent, events, _ = codex_agent
    error = {'message': 'private-provider-body', 'additionalDetails': 'private-stderr',
             'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 409}}}
    async def stream():
        if source == 'exception':
            raise TimeoutError('private-provider-body')
        if source == 'notification':
            yield sdk_event('error', {'error': error, 'willRetry': False})
        yield sdk_event('turn/completed', {'turn': {'status': 'failed',
            **({'error': error} if source == 'turn' else {})}})
    install_codex_client(monkeypatch, agent, stream)
    result = agent.run_conversation('Read the file', conversation_history=[], system_message='Moyai')
    failure = result['sdk_failure']
    assert result['failed'] and failure['sdk'] == 'codex'
    if source == 'exception':
        assert failure['exception_type'] == 'TimeoutError'
    else:
        assert failure['code'] == 'httpConnectionFailed' and failure['http_status'] == 409
        assert 'HTTP 409' in result['final_response']
        if source == 'notification':
            assert failure['will_retry'] is False
    assert [event[2]['phase'] for event in events if event[0] == 'error'] == ['sdk_failure']
    assert 'private-' not in json.dumps([result, events])


def test_retried_codex_notification_does_not_fail_success(codex_agent, monkeypatch):
    agent, events, _ = codex_agent
    async def stream():
        yield sdk_event('error', {'error': {'codexErrorInfo': 'serverOverloaded'}, 'willRetry': True})
        yield sdk_event('item/completed', {'item': {
            'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Done.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
    install_codex_client(monkeypatch, agent, stream)
    result = agent.run_conversation('Read', conversation_history=[], system_message='Moyai')
    assert result['completed'] and result['final_response'] == 'Done.'
    assert 'sdk_failure' not in result and not [e for e in events if e[0] == 'error']
