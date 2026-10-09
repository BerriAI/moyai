import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import tomllib
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import codex_harness
from sandbox.activity import ActivityReporter
from sandbox.broker_relay import BrokerRelay
from sandbox.context_store import ContextStore
from sandbox.harness_agent import TurnJournal
from sandbox.transport_recovery import MAX_TRANSPORT_ATTEMPTS, recovery_marker


@pytest.fixture
def codex_agent(monkeypatch, tmp_path, request):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'run-capability')
    events = []
    activity = ActivityReporter(lambda *args: events.append(args), tracing=True)
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    agent = codex_harness.CodexAgent(
        spec={'model': 'openai/gpt-6-astra', 'max_iterations': 6,
              'transport_attempt': getattr(request, 'param', 0)},
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


def install_codex_client(monkeypatch, agent, stream, late_items=(), late_notifications=None):
    observed = SimpleNamespace(homes=[], prompts=[], thread_options=[], steers=[], steer_error=None, exits=[])

    class Client:
        def __init__(self, config):
            home = Path(config.env['CODEX_HOME'])
            assert home.is_dir()
            (home / 'native-private-transcript').write_text('requester-native-secret')
            observed.homes.append(home)
            self.notifications = stream()
            self.turn_count = 0
            self.completed_turns = set()
            self.late_items = iter(late_items)

        async def __aenter__(self): return self
        async def __aexit__(self, *args): observed.exits.append(self.turn_count)
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
            from openai_codex.errors import TransportClosedError
            if turn_id != f'fresh-turn-{self.turn_count}' or turn_id in self.completed_turns:
                if late_notifications:
                    return late_notifications(turn_id, self.turn_count)
                try:
                    return next(self.late_items)
                except StopIteration:
                    raise TransportClosedError('Turn is no longer streaming') from None
            try:
                event = await anext(self.notifications)
            except StopAsyncIteration:
                raise TransportClosedError('Turn is no longer streaming') from None
            if event.method == 'turn/completed':
                self.completed_turns.add(turn_id)
            return event

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


@pytest.mark.parametrize('outcome', ['receipt', 'queued', 'unknown', 'unconfirmed', 'failed', 'stop', 'deadline'])
def test_native_compaction_keeps_receipts_private_and_respects_controls(codex_agent, outcome):
    from openai_codex.errors import TransportClosedError
    agent, events, store = codex_agent
    started, finished = native_item('commandExecution')
    agent.record_item(started, completed=False)
    pressure = {'input_tokens': 30000, 'input_budget': 20000}
    agent.context.relay.context_required = pressure
    compacted = {'type': 'contextCompaction', 'id': 'summary'}
    queued = [sdk_event('item/completed', {'item': finished})] if outcome == 'queued' else []
    messages = [sdk_event('turn/started', {'turn': {'id': 'compact'}}),
        sdk_event('rawResponseItem/completed', {'item': {'type': 'custom_tool_call_output',
            'call_id': started['id'], 'output': 'raw-private-marker'}}),
        sdk_event('item/completed', {'item': {'type': 'agentMessage', 'id': 'private-summary',
            'text': 'native-private-marker'}})]
    if outcome == 'receipt':
        messages.extend([sdk_event('item/completed', {'item': finished})] * 2)
    if outcome != 'unconfirmed':
        messages.append(sdk_event('item/completed', {'item': compacted}))
    messages.append(sdk_event('turn/completed', {'turn': {
        'id': 'compact', 'status': 'failed' if outcome == 'failed' else 'completed'}}))
    cleanup = []
    closed = asyncio.Event()
    def wake():
        cleanup.append('wake')
        closed.set()
    route = SimpleNamespace(wake_notification_reader=wake)

    class Client:
        def register_goal_operation(self, thread_id):
            assert thread_id == 'same-thread'
            return route
        def unregister_goal_operation(self, value):
            assert value is route
            cleanup.append('unregister')
        async def thread_compact(self, thread_id):
            assert agent.context.relay.native_compacting
            assert agent.before_model() == (outcome != 'stop')
        async def next_turn_notification(self, turn_id):
            assert turn_id == 'original'
            if queued:
                return queued.pop()
            raise TransportClosedError('Turn is no longer streaming')
        async def next_goal_notification(self, value):
            if outcome == 'stop':
                agent.interrupt()
            if not messages or outcome in {'stop', 'deadline'}:
                await closed.wait()
                raise TransportClosedError('Compaction reader closed')
            return messages.pop(0)

    async def run():
        if outcome == 'deadline':
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await agent.compact_context(Client(), 'same-thread', ['original'])
        else:
            assert await agent.compact_context(Client(), 'same-thread', ['original']) == (outcome not in {'unconfirmed', 'failed', 'stop'})
    asyncio.run(run())
    assert bool(agent.journal.pending) == bool(store.pending) == (outcome not in {'receipt', 'queued'})
    assert agent.journal.completed_tools == int(outcome in {'receipt', 'queued'})
    assert agent.model_calls == int(outcome != 'stop') and cleanup == ['unregister', 'wake']
    assert agent.context.relay.context_required is pressure and not agent.context.relay.native_compacting
    saved = json.dumps([agent.journal.messages, events, store.history()])
    assert 'raw-private-marker' not in saved and 'native-private-marker' not in saved


@pytest.mark.parametrize('outcome', ['receipt', 'unknown', 'stop', 'deadline'])
def test_pending_receipt_drain_preserves_unknowns_and_respects_stop(codex_agent, monkeypatch, outcome):
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
                    await agent.settle_pending_receipts(Client(), ['settlement', 'original'])
        else:
            await agent.settle_pending_receipts(Client(), ['settlement', 'original'])

    asyncio.run(drain())
    assert bool(agent.journal.pending) == bool(store.pending) == (outcome != 'receipt')
    assert agent.journal.completed_tools == int(outcome == 'receipt')
    assert agent.model_calls == 0
    assert 'raw-private-marker' not in json.dumps([agent.journal.messages, events, store.history()])


def install_transport_relay(agent):
    relay = agent.context.relay
    relay.failure_lock = threading.Lock()
    relay.last_failure, relay.last_error = None, ''
    relay.model_failed = relay.uncertain_tool = False
    relay.context_required = None
    resumed = []

    def resume(observed, *, live=False):
        allowed = BrokerRelay.resume_model(relay, observed, live=live)
        if allowed:
            resumed.append(observed['request_id'])
        return allowed

    relay.resume_model = resume
    relay.model_ready = lambda *, timeout: True
    return relay, resumed


def model_failure(relay, request_id='failed-model'):
    failure = {'version': 1, 'route': '/v1/responses', 'http_status': 502,
               'request_id': request_id, 'transient': True, 'response_started': False,
               'uncertain_tool': False}
    relay.last_failure, relay.last_error, relay.model_failed = failure, 'Connection failed.', True
    return failure


def test_live_transport_keeps_tools_all_prior_turns_and_queued_input(codex_agent, monkeypatch):
    from openai_codex.errors import TransportClosedError
    agent, events, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    first, first_done = native_item('commandExecution')
    second, second_done = ({**item, 'id': 'second-command'} for item in (first, first_done))
    delivered, delays, namespaces = set(), [], []
    sleep = asyncio.sleep

    async def backoff(seconds):
        if seconds >= 2:
            delays.append(seconds)
            assert agent.journal.pending and relay.model_failed
            if len(delays) == 1:
                assert agent.accept_input({'id': 91, 'content': 'Keep the existing preview running until tests finish.'})
        await sleep(0)

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', backoff)

    def late(turn_id, current_turn):
        # Both receipts arrive as settlement starts, after two transport
        # continuations. Reading only the latest retired turn loses them.
        item = {'fresh-turn-1': first_done, 'fresh-turn-2': second_done}.get(turn_id)
        if current_turn == 4 and item and turn_id not in delivered:
            delivered.add(turn_id)
            return sdk_event('item/completed', {'item': item})
        raise TransportClosedError('Turn is no longer streaming')

    async def stream():
        for index, item in enumerate((first, second), 1):
            assert agent.before_model()
            namespaces.append(agent.journal.call_namespace)
            yield sdk_event('item/started', {'item': item})
            model_failure(relay, f'failed-{index}')
            yield sdk_event('turn/completed', {'turn': {'status': 'failed',
                'error': {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}}})
        assert len(observed.prompts) == 3 and agent.journal.pending
        assert agent.before_model()
        yield sdk_event('item/completed', {'item': {'id': 'premature', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Unconfirmed answer.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})
        # Compaction's receipt pump also serves transport recovery: completions
        # on older turns are saved before the next model request is admitted.
        assert len(observed.prompts) == 4 and not agent.journal.pending
        assert agent.journal.completed_tools == 2
        assert agent.before_model()
        namespaces.append(agent.journal.call_namespace)
        yield sdk_event('item/completed', {'item': {'id': 'confirmed', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Existing work confirmed.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream, late_notifications=late)
    result = agent.run_conversation('Finish this task.', conversation_history=[], system_message='Moyai')
    assert result['completed'] and result['final_response'] == 'Existing work confirmed.'
    assert len(observed.homes) == len(observed.thread_options) == 1
    assert resumed == ['failed-1', 'failed-2'] and delays == [2, 4]
    assert agent.transport_attempt == 2 and agent.model_calls == 4
    assert len(set(namespaces)) == 1 and not agent.journal.pending and not store.pending
    assert delivered == {'fresh-turn-1', 'fresh-turn-2'}
    assert sum('Keep the existing preview' in prompt for prompt in observed.prompts) == 1
    assert sum('Keep the existing preview' in (m.get('content') or '') for m in result['messages']) == 1
    receipts = [m for m in result['messages'] if m.get('role') == 'tool']
    assert len(receipts) == len({m['tool_call_id'] for m in receipts}) == 2
    assert sum(e[2]['phase'] == 'started' for e in events if e[0] == 'tool') == 2
    assert 'Unconfirmed answer.' not in json.dumps([result, store.history()])
    assert not relay.model_failed and not relay.last_error and relay.last_failure is None


@pytest.mark.parametrize('codex_agent', [0, 2, MAX_TRANSPORT_ATTEMPTS], indirect=True)
def test_live_transport_consumes_only_remaining_original_turn_budget(codex_agent, monkeypatch):
    from openai_codex.errors import TransportClosedError
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    initial = agent.context.spec['transport_attempt']
    assert agent.transport_attempt == initial
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.01)
    sleep, delays = asyncio.sleep, []

    async def backoff(seconds):
        if seconds >= 2:
            delays.append(seconds)
        await sleep(0)

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', backoff)

    async def stream():
        yield sdk_event('item/started', {'item': native_item('commandExecution')[0]})
        for turn in range(1, MAX_TRANSPORT_ATTEMPTS + 3):
            if len(observed.prompts) != turn:
                raise TransportClosedError('Completed turn has no late receipt')
            assert agent.before_model()
            model_failure(relay, f'failure-{turn}')
            yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    remaining = MAX_TRANSPORT_ATTEMPTS - initial
    assert result['failed'] and agent.journal.pending and store.pending
    assert len(observed.prompts) == agent.model_calls == remaining + 1
    assert len(resumed) == remaining and agent.transport_attempt == MAX_TRANSPORT_ATTEMPTS
    assert delays == [2 ** attempt for attempt in range(initial + 1, MAX_TRANSPORT_ATTEMPTS + 1)]
    assert len(observed.homes) == len(observed.thread_options) == 1
    assert recovery_marker(agent, result) is None


@pytest.mark.parametrize('codex_agent', [2], indirect=True)
def test_transport_budget_survives_native_invocation_reentry(codex_agent, monkeypatch):
    from openai_codex.errors import TransportClosedError
    agent, _, _ = codex_agent
    relay, resumed = install_transport_relay(agent)
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.01)
    sleep, delays, invocation = asyncio.sleep, [], 0

    async def backoff(seconds):
        if seconds >= 2:
            delays.append(seconds)
        await sleep(0)

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', backoff)

    async def stream():
        nonlocal invocation
        invocation += 1
        assert agent.transport_attempt == (2 if invocation == 1 else 3)
        started, finished = native_item('commandExecution')
        yield sdk_event('item/started', {'item': started})
        assert agent.before_model()
        model_failure(relay, f'failure-{invocation}')
        yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})
        if invocation == 2:
            raise TransportClosedError('Unknown tool outcome')
        assert agent.before_model()
        yield sdk_event('item/completed', {'item': finished})
        yield sdk_event('item/completed', {'item': {'id': 'answer', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'First invocation finished.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    first = asyncio.run(agent._run('Work', 'Moyai'))
    second = asyncio.run(agent._run('Continue after a context handoff.', 'Moyai'))
    assert first['completed'] and second['failed']
    assert resumed == ['failure-1'] and delays == [8] and agent.transport_attempt == 3
    assert len(observed.homes) == 2 and len(observed.prompts) == 3
    assert agent.journal.completed_tools == 1 and len(agent.journal.pending) == 1


@pytest.mark.parametrize('guard', ['permanent', 'partial', 'tool_failure', 'uncertain_tool',
    'boundary', 'stop', 'missing_failure', 'native_interrupted', 'native_unknown',
    'stop_during_wait', 'boundary_during_wait',
    'model_limit', 'model_limit_during_wait', 'uncertain_during_wait', 'replaced_failure', 'deadline'])
@pytest.mark.parametrize('late_receipt', [False, True])
def test_live_transport_cannot_bypass_recovery_guards(codex_agent, monkeypatch, guard, late_receipt):
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.01)
    sleep, receipt_waits = asyncio.sleep, []
    if guard == 'deadline':
        agent.context.spec['timeout'] = 0.02
    started, finished = native_item('commandExecution')

    async def backoff(seconds):
        if seconds >= 2:
            if guard == 'stop_during_wait': agent.interrupt()
            if guard == 'boundary_during_wait': agent.boundary_failed = True
            if guard == 'model_limit_during_wait': agent.context.spec['max_iterations'] = 1
            if guard == 'uncertain_during_wait': relay.uncertain_tool = True
            if guard == 'replaced_failure': relay.last_failure = dict(relay.last_failure)
            if guard == 'deadline': await sleep(1)
        else:
            receipt_waits.append(seconds)
        await sleep(0)

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', backoff)

    async def stream():
        assert agent.before_model()
        yield sdk_event('item/started', {'item': started})
        failure = model_failure(relay)
        if guard == 'permanent': failure['transient'] = False
        if guard == 'partial': failure['response_started'] = True
        if guard == 'tool_failure': failure.update(route='/tools/call', uncertain_tool=True)
        if guard == 'uncertain_tool': relay.uncertain_tool = True
        if guard == 'boundary': agent.boundary_failed = True
        if guard == 'stop': agent.interrupt()
        if guard == 'model_limit': agent.context.spec['max_iterations'] = 1
        if guard == 'missing_failure': relay.last_failure = None
        status = {'native_interrupted': 'interrupted', 'native_unknown': 'unknown'}.get(guard, 'failed')
        yield sdk_event('turn/completed', {'turn': {'status': status,
            'error': {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}}})
    # Already queued by the native runtime: rejecting another inference must
    # still save its authoritative result from the completed turn's channel.
    late_items = [sdk_event('item/completed', {'item': finished})] if late_receipt else []
    observed = install_codex_client(monkeypatch, agent, stream, late_items=late_items)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert not result['completed'] and not resumed and len(observed.prompts) == 1
    assert len(observed.homes) == len(observed.thread_options) == 1
    assert agent.model_calls == 1 and relay.model_failed
    receipt_saved = late_receipt and guard != 'deadline'
    assert bool(agent.journal.pending) == bool(store.pending) == (not receipt_saved)
    receipts = [json.loads(row[0]) for row in store.db.execute('SELECT message FROM journal')]
    receipts = [message for message in receipts if message.get('role') == 'tool']
    assert len(receipts) == agent.journal.completed_tools == int(receipt_saved)
    if receipts:
        assert receipts[0]['content'] == 'proof'
    assert result['interrupted'] == (guard in {'stop', 'stop_during_wait'})
    if guard in {'stop', 'stop_during_wait'}:
        assert not receipt_waits  # Stop permits queued receipts, never a grace wait.
    if not receipt_saved:
        assert recovery_marker(agent, result) is None
    elif guard in {'permanent', 'partial', 'tool_failure', 'uncertain_tool', 'boundary',
                   'stop', 'missing_failure', 'stop_during_wait', 'boundary_during_wait', 'uncertain_during_wait'}:
        assert recovery_marker(agent, result) is None
    if guard == 'deadline':
        assert result['sdk_failure']['exception_type'] == 'TimeoutError'


@pytest.mark.parametrize('tools', ['none', 'settled', 'pending'])
@pytest.mark.parametrize('stream_started', [False, True])
def test_live_transport_continues_same_thread_across_tool_and_stream_states(
        codex_agent, monkeypatch, tools, stream_started):
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    started, finished = native_item('commandExecution')
    sleep = asyncio.sleep

    async def fast_backoff(seconds):
        await sleep(0)

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', fast_backoff)

    async def stream():
        assert agent.before_model()
        if tools != 'none':
            yield sdk_event('item/started', {'item': started})
        if tools == 'settled':
            yield sdk_event('item/completed', {'item': finished})
        failure = model_failure(relay)
        if stream_started:
            failure.update(http_status=200, response_started=True, transport_interrupted=True)
        # A surviving native thread can continue a known interrupted stream;
        # recreating a client from public receipts must keep its stricter gate.
        assert bool(recovery_marker(agent, {'failed': True})) == (tools != 'pending' and not stream_started)
        yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})
        assert len(observed.prompts) == 2 and not observed.exits
        assert agent.before_model()
        if tools == 'pending':
            yield sdk_event('item/completed', {'item': finished})
        yield sdk_event('item/completed', {'item': {'id': 'answer', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Continued existing work.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert result['completed'] and result['final_response'] == 'Continued existing work.'
    assert resumed == ['failed-model'] and agent.transport_attempt == 1 and agent.model_calls == 2
    assert len(observed.homes) == len(observed.thread_options) == len(observed.exits) == 1
    assert not agent.journal.pending and not store.pending
    assert agent.journal.completed_tools == int(tools != 'none')


def test_broker_readiness_wait_saves_late_receipt_before_resuming(codex_agent, monkeypatch):
    from openai_codex.errors import TransportClosedError
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    agent.context.spec['transport_recovery_seconds'] = 5
    started, finished = native_item('commandExecution')
    sleep, probes, delivered = asyncio.sleep, [], False

    async def fast_backoff(seconds):
        await sleep(0 if seconds >= 2 else min(seconds, 0.005))

    def model_ready(*, timeout):
        probes.append(timeout)
        assert 0 < timeout <= 5
        assert agent.model_calls == 1 and len(observed.prompts) == 1
        assert len(observed.homes) == len(observed.thread_options) == 1 and not observed.exits
        # Returning the broker is independent of native receipt delivery. This
        # stays unavailable until the live client has drained its completed queue.
        return agent.journal.completed_tools == 1

    def late(turn_id, current_turn):
        nonlocal delivered
        if probes and not delivered:
            assert current_turn == 1 and not resumed and relay.model_failed
            delivered = True
            return sdk_event('item/completed', {'item': finished})
        raise TransportClosedError('Completed turn has no further receipt')

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', fast_backoff)
    relay.model_ready = model_ready

    async def stream():
        assert agent.before_model()
        yield sdk_event('item/started', {'item': started})
        model_failure(relay)
        yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})
        assert delivered and not agent.journal.pending and not store.pending
        assert agent.before_model()
        yield sdk_event('item/completed', {'item': {'id': 'answer', 'type': 'agentMessage',
            'phase': 'final_answer', 'text': 'Recovered with the original receipt.'}})
        yield sdk_event('turn/completed', {'turn': {'status': 'completed'}})

    observed = install_codex_client(monkeypatch, agent, stream,
                                   late_notifications=late)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert result['completed'] and len(probes) >= 2
    assert resumed == ['failed-model'] and agent.transport_attempt == 1 and agent.model_calls == 2
    receipts = [json.loads(row[0]) for row in store.db.execute('SELECT message FROM journal')]
    receipts = [message for message in receipts if message.get('role') == 'tool']
    assert len(receipts) == agent.journal.completed_tools == 1
    assert receipts[0]['content'] == 'proof'
    assert len(observed.homes) == len(observed.thread_options) == len(observed.exits) == 1


@pytest.mark.parametrize('tools', ['pending', 'settled'])
@pytest.mark.parametrize('reason', ['unauthorized', 'forbidden', 'invalid', 'stop',
    'boundary', 'context_required', 'model_limit', 'uncertain_tool', 'replaced_failure',
    'recovery_deadline', 'task_deadline'])
def test_broker_readiness_cannot_override_recovery_boundaries(codex_agent, monkeypatch, reason, tools):
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    sleep, probes = asyncio.sleep, []
    agent.context.spec['transport_recovery_seconds'] = 0.04 if reason == 'recovery_deadline' else 5
    if reason == 'task_deadline':
        agent.context.spec['timeout'] = 0.04
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.01)

    async def fast_backoff(seconds):
        await sleep(0 if seconds >= 2 else min(seconds, 0.005))

    def model_ready(*, timeout):
        probes.append(timeout)
        assert not observed.exits and agent.model_calls == 1
        if reason in {'unauthorized', 'forbidden'}:
            raise urllib.error.HTTPError('http://broker/v1/models',
                401 if reason == 'unauthorized' else 403, 'Unauthorized', {}, None)
        if reason == 'invalid':
            raise ValueError('Invalid broker model list')
        if reason == 'stop': agent.interrupt()
        if reason == 'boundary': agent.boundary_failed = True
        if reason == 'context_required': relay.context_required = {'reason': 'context handoff needed'}
        if reason == 'model_limit': agent.context.spec['max_iterations'] = 1
        if reason == 'uncertain_tool': relay.uncertain_tool = True
        if reason == 'replaced_failure': relay.last_failure = dict(relay.last_failure)
        return reason not in {'recovery_deadline', 'task_deadline'}

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', fast_backoff)
    relay.model_ready = model_ready

    async def stream():
        assert agent.before_model()
        started, finished = native_item('commandExecution')
        yield sdk_event('item/started', {'item': started})
        if tools == 'settled':
            yield sdk_event('item/completed', {'item': finished})
        model_failure(relay)
        yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})

    observed = install_codex_client(monkeypatch, agent, stream)
    result = asyncio.run(agent._run('Work', 'Moyai'))
    assert probes and not result['completed'] and not resumed
    assert len(observed.prompts) == agent.model_calls == 1
    assert len(observed.homes) == len(observed.thread_options) == len(observed.exits) == 1
    assert bool(agent.journal.pending) == bool(store.pending) == (tools == 'pending')
    assert relay.model_failed
    assert result['interrupted'] == (reason == 'stop')
    if tools == 'pending' or reason in {'unauthorized', 'forbidden', 'invalid', 'stop',
                                       'boundary', 'uncertain_tool', 'recovery_deadline'}:
        # Permanent readiness rejection must also close the cold-recovery gate
        # when every receipt is saved. The durable owner separately enforces the
        # original deadline/call cap and context handoff eligibility.
        assert recovery_marker(agent, result) is None
    if reason == 'task_deadline':
        assert result['sdk_failure']['exception_type'] == 'TimeoutError'


def test_cancellation_during_readiness_cannot_start_another_native_turn(codex_agent, monkeypatch):
    agent, _, store = codex_agent
    relay, resumed = install_transport_relay(agent)
    probe_started, release_probe = threading.Event(), threading.Event()
    sleep = asyncio.sleep

    async def fast_backoff(seconds):
        await sleep(0 if seconds >= 2 else min(seconds, 0.005))

    def model_ready(*, timeout):
        assert 0 < timeout <= 1
        probe_started.set()
        assert release_probe.wait(timeout=1)
        return True

    monkeypatch.setattr(codex_harness.asyncio, 'sleep', fast_backoff)
    relay.model_ready = model_ready

    async def stream():
        assert agent.before_model()
        yield sdk_event('item/started', {'item': native_item('commandExecution')[0]})
        model_failure(relay)
        yield sdk_event('turn/completed', {'turn': {'status': 'failed'}})

    observed = install_codex_client(monkeypatch, agent, stream)

    async def cancel_while_waiting():
        task = asyncio.create_task(agent._run('Work', 'Moyai'))
        try:
            async with asyncio.timeout(0.5):
                while not probe_started.is_set():
                    await sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert observed.exits == [1] and not resumed
        finally:
            release_probe.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(cancel_while_waiting())
    assert len(observed.prompts) == agent.model_calls == 1
    assert agent.journal.pending and store.pending and relay.model_failed


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


@pytest.mark.parametrize('arrival', ['request', 'summary', 'handoff'])
def test_compaction_drains_late_receipts_with_pinned_sdk_routing(codex_agent, monkeypatch, arrival):
    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.models import Notification, UnknownNotification
    agent, events, store = codex_agent
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.5)
    started, finished = native_item('commandExecution')
    agent.record_item(started, completed=False)
    routed = []

    class Client(AsyncCodexClient):
        def emit(self, method, body, turn_id='original'):
            self._sync._router.route_notification(Notification(method, UnknownNotification({
                'threadId': 'same-thread', 'turnId': turn_id, **body})))

        async def thread_compact(self, thread_id):
            self.emit('turn/started', {'turn': {'id': 'compact'}}, 'compact')
            if arrival == 'request':
                self.emit('item/completed', {'item': finished})
                # Admission can wait for a receipt while the RPC is outstanding.
                assert await asyncio.to_thread(agent.before_model, json.dumps({'input': [
                    {'type': 'function_call_output', 'call_id': started['id'], 'output': 'wire-only'}]}))
            if arrival == 'summary':
                self.emit('item/completed', {'item': finished})
            self.emit('item/completed', {'item': {'type': 'contextCompaction', 'id': 'summary'}}, 'compact')
            self.emit('turn/completed', {'turn': {'id': 'compact', 'status': 'completed'}}, 'compact')
            if arrival == 'handoff':
                # The SDK reader can queue a tool completion behind the compact
                # turn's completion before the adapter closes the goal route.
                self.emit('item/completed', {'item': finished})

        async def next_goal_notification(self, route):
            event = await super().next_goal_notification(route)
            routed.append(event.method)
            return event

    async def run():
        client = Client()
        client._sync._router.register_turn('original')
        client.emit('turn/completed', {'turn': {'id': 'original', 'status': 'failed'}})
        assert await agent.compact_context(client, 'same-thread', ['original'])
        assert not client._sync._router.has_goal('same-thread')

    asyncio.run(run())
    assert not agent.boundary_failed and not store.pending
    assert agent.journal.completed_tools == 1
    assert routed.count('item/completed') == 2
    assert sum(message['role'] == 'tool' for message in agent.journal.messages) == 1


def test_turn_start_keeps_prior_receipts_flowing(codex_agent, monkeypatch):
    from openai_codex.errors import TransportClosedError
    agent, events, store = codex_agent
    monkeypatch.setattr(codex_harness, 'RECEIPT_TIMEOUT_SECONDS', 0.5)
    started, finished = native_item('commandExecution')
    queued = []
    original = [sdk_event('item/started', {'item': started}),
                sdk_event('turn/completed', {'turn': {'status': 'failed'}})]
    continued = [sdk_event('item/completed', {'item': {'type': 'agentMessage',
        'id': 'answer', 'phase': 'final_answer', 'text': 'Confirmed answer.'}}),
        sdk_event('turn/completed', {'turn': {'status': 'completed'}})]

    class Client:
        def __init__(self, config): self.turns = 0
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def thread_start(self, options):
            return SimpleNamespace(thread=SimpleNamespace(id='same-thread'))
        async def turn_start(self, thread_id, prompt):
            self.turns += 1
            if self.turns == 1:
                return SimpleNamespace(turn=SimpleNamespace(id='original'))
            queued.append(sdk_event('item/completed', {'item': finished}))
            admitted = await asyncio.to_thread(agent.before_model, json.dumps({'input': [
                {'type': 'function_call_output', 'call_id': started['id'], 'output': 'wire-only'}]}))
            assert admitted
            return SimpleNamespace(turn=SimpleNamespace(id='continued'))

        async def next_turn_notification(self, turn_id):
            if turn_id == 'continued':
                return continued.pop(0)
            if original:
                agent.context.relay.context_required = {'input_tokens': 30000, 'input_budget': 20000}
                return original.pop(0)
            if queued:
                return queued.pop(0)
            raise TransportClosedError('Turn is no longer streaming')

    async def compact(*args): return True
    monkeypatch.setattr(agent, 'compact_context', compact)
    monkeypatch.setattr(agent, 'validate', lambda: None)
    monkeypatch.setattr('openai_codex.async_client.AsyncCodexClient', Client)
    result = agent.run_conversation('Work', conversation_history=[], system_message='Moyai')
    assert result['completed'] and result['final_response'] == 'Confirmed answer.'
    assert not agent.boundary_failed and not store.pending
    assert agent.journal.completed_tools == 1


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


def test_private_mcp_tool_names_preserve_existing_redaction(codex_agent):
    agent, events, store = codex_agent
    # Private trace payloads are configurable; this case exercises omission.
    agent.context.activity.omit_private_tool_payloads = True
    item = {'id': 'memory-call', 'type': 'mcpToolCall', 'server': 'moyai', 'tool': 'memory_save',
            'arguments': {'content': 'requester-private-marker'}, 'status': 'inProgress'}
    agent.record_item(item, completed=False)
    agent.record_item({**item, 'status': 'completed', 'result': {
        'content': [{'type': 'text', 'text': '{"saved":true}'}]}}, completed=True)
    saved = ''.join(row[0] for row in store.db.execute('SELECT message FROM journal'))
    assert 'requester-private-marker' not in saved
    assert 'requester-private-marker' not in json.dumps(events)
    assert next(event for event in events if event[0] == 'trace')[2]['input'] == '[private tool payload omitted]'
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
