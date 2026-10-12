"""Publication races and prompt recovery against the real broker and database."""
import asyncio
from dataclasses import replace
import json
import threading
import time

import pytest

from app.deepagents_workflow import TurnScope
from app.security import digest
from test_deepagents_runtime import durable_app  # noqa: F401


@pytest.fixture
def active(durable_app):
    manager, store = durable_app.state.manager, durable_app.state.store
    run = store.create_run('Keep working for one hour.', '', 'modal', [], chat_enabled=True,
                           harness='deepagents-temporal', model='test-model', user_id='owner')
    manager.submit(run)
    message = store.claim_message(run['id'])
    manager.save(run['id'], {'message_id': message['id'], 'segment': 0, 'turn_started': time.time()})
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], message['id'])))
    scope = TurnScope(run['id'], message['id'], actor_id='owner')
    manager.deepagents_runtime.journal.exchange(store.run(run['id']), {'action': 'import',
        'manifest': {'id': 'empty', 'count': 0}, 'entries': [], 'complete': True})
    return manager, store, scope


def publications(store, scope):
    return (store.rows('SELECT * FROM session_items WHERE run_id=?', (scope.run_id,)),
            [e for e in store.events(scope.run_id, limit=1000) if e['kind'] == 'message'])


@pytest.mark.parametrize('transition', ['pending', 'accepted', 'stop', 'segment', 'actor', 'model'])
async def test_late_publication_and_stream_are_fenced(active, transition):
    manager, store, scope = active
    if transition in {'pending', 'accepted'}:
        message, _ = store.enqueue_message(scope.run_id, 'Correction', 'correction', user_id='owner', send_immediately=True)
        if transition == 'accepted':
            control = manager.message_queue.live_control(scope.run_id, scope.message_id, [])
            assert control['input']['id'] == message['id']
            manager.message_queue.acknowledge(scope.run_id, scope.message_id, [message['id']])
    elif transition == 'stop':
        store.update_run(scope.run_id, status='stopping')
    elif transition == 'segment':
        manager.save(scope.run_id, {**manager.state(scope.run_id), 'segment': 1})
    elif transition == 'actor':
        store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('another-user', scope.run_id))
    else:
        scope = replace(scope, model='old-model')
    deliveries = []
    store.public_delivery = lambda conn, run_id: deliveries.append(run_id)
    await manager.deepagents_runtime.publish(scope, 'answer', 'Obsolete answer', scope.message_id)
    manager.deepagents_runtime.stream_text(scope, 'stream', scope.message_id, 'Obsolete fragment')
    assert publications(store, scope) == ([], [])
    assert deliveries == []


async def test_ordinary_queued_followup_allows_current_answer_and_retry_is_idempotent(active):
    manager, store, scope = active
    store.enqueue_message(scope.run_id, 'Next question', 'next', user_id='owner')
    observed = []
    def delivery(conn, run_id):
        # The outbox sees both records inside the publication transaction.
        observed.append((len(conn.execute('SELECT * FROM session_items WHERE run_id=?', (run_id,)).fetchall()),
                         len(conn.execute("SELECT * FROM events WHERE run_id=? AND kind='message'", (run_id,)).fetchall())))
    store.public_delivery = delivery
    for _ in range(2):
        await manager.deepagents_runtime.publish(scope, 'answer', 'Current answer', scope.message_id)
    journal, events = publications(store, scope)
    assert len(journal) == len(events) == 1
    assert observed == [(1, 1), (1, 1)]


async def test_failed_delivery_rolls_back_journal_and_event_before_retry(active):
    manager, store, scope = active
    def fail(conn, run_id):
        raise RuntimeError('Outbox unavailable')
    store.public_delivery = fail
    with pytest.raises(RuntimeError, match='Outbox unavailable'):
        await manager.deepagents_runtime.publish(scope, 'answer', 'Saved together', scope.message_id)
    assert publications(store, scope) == ([], [])
    store.public_delivery = None
    await manager.deepagents_runtime.publish(scope, 'answer', 'Saved together', scope.message_id)
    assert tuple(map(len, publications(store, scope))) == (1, 1)


async def test_cancelled_publication_drains_transaction_and_retry_does_not_duplicate(active, monkeypatch):
    manager, store, scope = active
    entered, release = threading.Event(), threading.Event()
    exchange = manager.deepagents_runtime.journal.exchange_in
    def held(conn, caller, body):
        result = exchange(conn, caller, body)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(manager.deepagents_runtime.journal, 'exchange_in', held)
    task = asyncio.create_task(manager.deepagents_runtime.publish(scope, 'answer', 'Saved together', scope.message_id))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await manager.deepagents_runtime.publish(scope, 'answer', 'Saved together', scope.message_id)
    assert tuple(map(len, publications(store, scope))) == (1, 1)


@pytest.mark.parametrize('has_goal', [False, True])
async def test_prepare_restores_current_goal_initial_followup_and_checkpoint(active, has_goal):
    manager, store, scope = active
    if has_goal:
        caller = store.run(scope.run_id)
        store.goals.exchange(caller, {'action': 'create', 'source_input_id': scope.message_id,
            'objective': 'Verify all acceptance criteria.', 'budget': {'kind': 'timebox', 'seconds': 3600}})
        store.goals.exchange(caller, {'action': 'tick', 'segment': 'previous-worker', 'elapsed': 123}, internal=True)
        store.goals.exchange(caller, {'action': 'progress', 'note': 'First check passed.',
                                      'remaining': 'Verify recovery.', 'evidence': []})
    for phase in ('initial', 'checkpoint', 'followup'):
        if phase == 'checkpoint':
            checkpoint = manager.payloads.put(json.dumps({'scope': [scope.run_id, scope.message_id, 'owner', 'test-model'],
                                                          'messages': [], 'applied': []}).encode())
            scope = replace(scope, segment=1)
            manager.save(scope.run_id, {**manager.state(scope.run_id), 'segment': 1, 'deepagents_checkpoint': checkpoint})
        elif phase == 'followup':
            store.finish_message(scope.run_id, scope.message_id, 'Progress saved.')
            store.enqueue_message(scope.run_id, 'Continue the existing goal.', 'followup', user_id='owner')
            message = store.claim_message(scope.run_id)
            scope = replace(scope, message_id=message['id'], segment=0)
            manager.save(scope.run_id, {'message_id': scope.message_id, 'segment': 0, 'turn_started': time.time()})
            store.update_run(scope.run_id, status='running', token_hash=digest(manager.token(scope.run_id, scope.message_id)))
        prepared = await manager.deepagents_runtime.prepare(scope)
        embedded = json.loads(prepared.instructions.split('Current durable goal (reference data):\n')[1])
        goal = store.goals.exchange(store.run(scope.run_id), {'action': 'get'})['goal']
        assert embedded == goal
        if has_goal:
            assert embedded['elapsed_seconds'] == 123
            assert embedded['budget']['seconds'] == 3600
