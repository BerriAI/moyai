"""Real broker/SQL dispatch ordering; only the external side effect is a fixture."""
import asyncio
import json
import threading

import pytest
from temporalio.exceptions import ApplicationError

from app.connector_errors import ConnectorError
from app.message_queue import MessageQueue
from app.tool_dispatch import ToolDispatch
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app  # noqa: F401


@pytest.fixture
def broker(durable_app, active, monkeypatch):
    manager, store, scope = active
    store.execute('UPDATE runs SET plugins=? WHERE id=?', (json.dumps(['github', 'notion']), scope.run_id))
    monkeypatch.setattr(durable_app.state.connectors, 'allowed', lambda *a, **kw: True)
    calls = []

    async def connected(*args, **kwargs):
        calls.append(args)
        return {'id': 'confirmed-fixture-write'}

    monkeypatch.setattr(durable_app.state.connectors, 'call', connected)
    monkeypatch.setattr(durable_app.state.connectors.github, 'call', connected)
    return durable_app, manager, store, scope, calls


def body(scope, *, name='notion_append', identity='a' * 32):
    arguments = ({'repository': 'BerriAI/moyai', 'request_key': 'fixture-write', 'title': 'Fixture'}
                 if name == 'github_create_issue' else {'page_id': 'b' * 32, 'text': 'Fixture write'})
    return {'name': name, 'arguments': arguments, 'input_id': scope.message_id, 'request_id': identity}


def correction(manager, store, scope, *, accept=True):
    message, _ = store.enqueue_message(scope.run_id, 'Cancel the obsolete write.', 'correction',
                                      user_id='owner', send_immediately=True)
    if accept:
        manager.message_queue.live_control(scope.run_id, scope.message_id, [])
        manager.message_queue.acknowledge(scope.run_id, scope.message_id, [message['id']])
    return message['id']


def receipts(store, scope):
    return [json.loads(row['data']) for row in store.rows(
        "SELECT data FROM events WHERE run_id=? AND kind='tool_dispatch' ORDER BY id", (scope.run_id,))]


@pytest.mark.parametrize('accept', [False, True])
async def test_correction_during_connection_refresh_cancels_unstarted_write(broker, monkeypatch, accept):
    app, manager, store, scope, calls = broker
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        entered.set()
        await release.wait()

    monkeypatch.setattr(app.state.connectors.github, 'ensure_connection', refresh)
    task = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call',
        body(scope, name='github_create_issue')))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        correction(manager, store, scope, accept=accept)
    finally:
        release.set()
    result = await task
    assert calls == []
    assert result['outcome'] == 'cancelled' and result['dispatched'] is False
    assert receipts(store, scope) == []


@pytest.mark.parametrize('transition', ['accepted', 'pending', 'stop', 'actor', 'model'])
@pytest.mark.parametrize('internal', [False, True])
async def test_input_and_execution_changes_before_claim_cancel_every_tool_family(broker, monkeypatch, transition, internal):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    claim = ToolDispatch.claim

    def delayed(self):
        entered.set()
        assert release.wait(5)
        return claim(self)

    monkeypatch.setattr(ToolDispatch, 'claim', delayed)
    monkeypatch.setattr(app.state.skills, 'call', lambda *a: calls.append(a) or {'content': 'obsolete'})
    request = body(scope)
    if internal:
        request.update(name='skills_search', arguments={'query': 'fixture'})
    task = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', request))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if transition in {'accepted', 'pending'}:
            correction(manager, store, scope, accept=transition == 'accepted')
        else:
            field, value = {'stop': ('status', 'stopping'), 'actor': ('active_user_id', 'another-user'),
                            'model': ('active_model', 'another-model')}[transition]
            store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, scope.run_id))
    finally:
        release.set()
    result = await task
    assert result['outcome'] == 'cancelled' and result['dispatched'] is False
    assert calls == [] and receipts(store, scope) == []


async def test_dispatch_transaction_serializes_with_acknowledgment(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    original = MessageQueue.current_input

    def checked(conn, *args):
        valid = original(conn, *args)
        entered.set()
        assert release.wait(5)
        return valid

    monkeypatch.setattr(MessageQueue, 'current_input', staticmethod(checked))
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    acknowledging = None
    attempted = threading.Event()

    def acknowledge():
        attempted.set()
        return correction(manager, store, scope)
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        acknowledging = asyncio.create_task(asyncio.to_thread(acknowledge))
        assert await asyncio.to_thread(attempted.wait, 3)
        await asyncio.sleep(.05)
        assert not acknowledging.done(), 'Input cannot commit between the dispatch check and receipt'
    finally:
        release.set()
    await sending
    await acknowledging
    assert len(calls) == 1
    assert receipts(store, scope)[0]['outcome'] == 'completed'


async def test_running_write_keeps_receipt_after_correction_without_replay(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = asyncio.Event(), asyncio.Event()

    async def connected(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return {'id': 'confirmed-fixture-write'}

    monkeypatch.setattr(app.state.connectors, 'call', connected)
    request = body(scope)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert receipts(store, scope)[0]['outcome'] == 'unknown'
        duplicate = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
        assert duplicate['outcome'] == 'unknown' and duplicate['retryable'] is False
        correction(manager, store, scope)
        assert not sending.done()
    finally:
        release.set()
    assert (await sending)['id'] == 'confirmed-fixture-write'
    # A fresh handler after replacement observes the same receipt, even after steering.
    repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    assert repeated['outcome'] == 'completed' and repeated['retryable'] is False
    assert len(calls) == 1 and len(receipts(store, scope)) == 1


async def test_cancelled_delivery_of_confirmed_receipt_preserves_completed_outcome(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    finish = ToolDispatch.finish

    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        finish(self, *args, **kwargs)

    monkeypatch.setattr(ToolDispatch, 'finish', held)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        sending.cancel()
        await asyncio.sleep(.02)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await sending
    # A disconnected HTTP client can finish before the server drains its write.
    async with asyncio.timeout(3):
        while receipts(store, scope)[0]['outcome'] != 'completed':
            await asyncio.sleep(.01)
    assert receipts(store, scope)[0]['outcome'] == 'completed' and len(calls) == 1


async def test_cancelled_running_write_stays_uncertain_and_cannot_be_replayed(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered = asyncio.Event()

    async def uncertain(*args, **kwargs):
        calls.append(args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.connectors, 'call', uncertain)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    await asyncio.wait_for(entered.wait(), 3)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    result = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    assert result['outcome'] == 'unknown' and result['retryable'] is False
    assert len(calls) == 1


async def test_request_identity_cannot_be_rebound_or_omitted(broker):
    app, manager, store, scope, calls = broker
    request = body(scope)
    await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    request['arguments']['text'] = 'A different write'
    with pytest.raises(ApplicationError, match='409'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    for field in ('input_id', 'request_id'):
        with pytest.raises(ApplicationError, match='422'):
            await manager.deepagents_runtime.broker(scope, '/tools/call', {k: v for k, v in request.items() if k != field})
    assert len(calls) == 1


@pytest.mark.parametrize('dispatched', [False, True])
async def test_duplicate_preserves_known_unsent_or_uncertain_provider_outcome(broker, monkeypatch, dispatched):
    app, manager, store, scope, calls = broker

    async def failed(*args, **kwargs):
        calls.append(args)
        raise ConnectorError('Fixture transport failure', dispatched=dispatched)

    monkeypatch.setattr(app.state.connectors, 'call', failed)
    original = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    duplicate = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    assert original['outcome'] == duplicate['outcome'] == ('unknown' if dispatched else 'rejected')
    assert original['dispatched'] == duplicate['dispatched'] == dispatched
    assert duplicate['outcome_uncertain'] == dispatched
    assert len(calls) == 1
