"""Control handoff latency and lifecycle tests with no provider calls."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.security import digest
from sandbox.continuation import ActiveTurnSteering
from sandbox.context_store import ContextStore
from sandbox.harness_agent import HarnessAgent, HarnessInputs, TurnJournal
from sandbox.harness_registry import resolve
from test_message_queue import queue, change  # noqa: F401
from test_workspace import workspace  # noqa: F401


def test_owned_inputs_persist_once_preserve_order_and_close(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'input-test')
    journal = TurnJournal([], 'Original request', store)
    inputs = HarnessInputs(journal)
    try:
        assert not inputs.accept({'id': True, 'content': 'Invalid identity'})
        assert not inputs.accept({'id': 0, 'content': 'Invalid identity'})
        assert inputs.accept({'id': 2, 'content': 'First'})
        assert inputs.accept({'id': 2, 'content': 'Duplicate'})
        assert inputs.accept({'id': 1, 'content': 'Second'})
        assert inputs.pending and not inputs.close_if_empty()
        expected = ['[User correction to the current task]\nFirst',
                    '[User correction to the current task]\nSecond']
        assert inputs.take() == expected
        assert not inputs.pending and inputs.take() == []
        assert inputs.close_if_empty()
        assert not inputs.accept({'id': 3, 'content': 'Too late'})
        assert [item['content'] for item in journal.messages] == ['Original request', *expected]
        restored = ContextStore(store.path, 'input-test')
        try:
            assert [json.loads(row[0]) for row in restored.db.execute(
                'SELECT message FROM journal ORDER BY seq')] == journal.messages
        finally:
            restored.close()
    finally:
        store.close()


def test_input_acceptance_and_completion_are_one_atomic_decision(monkeypatch):
    journal = TurnJournal([], 'Original request')
    inputs = HarnessInputs(journal)
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    append = journal.append
    def blocked_append(message):
        entered.set()
        assert release.wait(2)
        append(message)
    monkeypatch.setattr(journal, 'append', blocked_append)
    def finish():
        closing.set()
        return inputs.close_if_empty()
    with ThreadPoolExecutor() as pool:
        accepted = pool.submit(inputs.accept, {'id': 1, 'content': 'Correction'})
        try:
            assert entered.wait(1)
            completed = pool.submit(finish)
            assert closing.wait(1)
            assert not completed.done()
        finally:
            release.set()
        assert accepted.result(timeout=1)
        assert not completed.result(timeout=1)
    inputs.close()
    assert not inputs.accept({'id': 2, 'content': 'Too late'})
    assert inputs.take() == ['[User correction to the current task]\nCorrection']
    assert inputs.close_if_empty()


def test_failed_input_journal_never_acknowledges_delivery(monkeypatch):
    journal = TurnJournal([], 'Original request')
    inputs = HarnessInputs(journal)
    calls = []
    def control(body):
        calls.append(body)
        return {'input': {'id': 1, 'content': 'Correction'}, 'receipt_only_supported': True}
    def failed_append(message):
        raise OSError('Local journal unavailable')
    append = journal.append
    monkeypatch.setattr(journal, 'append', failed_append)
    steering = ActiveTurnSteering(SimpleNamespace(control=control))
    agent = SimpleNamespace(accept_input=inputs.accept)
    steering._poll(agent, boundary=False)
    assert not inputs.pending and steering.receipts() == []
    assert not any(body.get('receipt_only') for body in calls)
    assert journal.messages == [{'role': 'user', 'content': 'Original request'}]
    monkeypatch.setattr(journal, 'append', append)
    steering._poll(agent, boundary=False)
    assert inputs.pending and steering.receipts() == [1]
    assert calls[-1] == {'version': 2, 'applied': [1], 'receipt_only': True}


@pytest.mark.parametrize('harness', ['codex', 'claude-agent-sdk'])
def test_native_input_admission_rejects_startup_and_closed_inbox(harness):
    definition = resolve(harness)
    assert definition.live_steering
    agent = definition.create(spec={}, relay=SimpleNamespace(), config={}, activity=None,
                              step=lambda: None, cwd='/workspace')
    item = {'id': 1, 'content': 'Correction'}
    steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: {'input': item}))
    try:
        steering._poll(agent, boundary=False)
        assert steering.receipts() == []
        agent.journal = TurnJournal([], 'Original request')
        agent.inputs = HarnessInputs(agent.journal)
        agent.inputs.close()
        steering._poll(agent, boundary=True)
        assert steering.receipts() == []
        assert agent.journal.messages == [{'role': 'user', 'content': 'Original request'}]
    finally:
        steering.close()
        agent.close()


@pytest.mark.parametrize('owned_inbox', [False, True])
def test_only_native_redirect_supersedes_inflight_model_response(owned_inbox):
    journal = TurnJournal([], 'Original request')
    agent = SimpleNamespace(redirect=lambda text: True)
    agent.accept_input = lambda item: HarnessAgent.accept_input(agent, item)
    if owned_inbox:
        agent.inputs = HarnessInputs(journal)
    steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: {
        'input': {'id': 1, 'content': 'Correction'}}))
    with steering.model_wait() as generation:
        steering._poll(agent, boundary=False)
        assert steering.receipts() == [1]
        assert steering.cancelled(generation) is not owned_inbox


@pytest.mark.parametrize('stage', ['control', 'attachments'])
def test_slow_io_does_not_block_model_progress_or_close(stage):
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        assert release.wait(3)
    def control(body):
        if stage == 'control':
            block()
        return {'input': {'id': 1, 'content': 'Correction'}}
    def prepare(item):
        if stage == 'attachments':
            block()
    steering = ActiveTurnSteering(SimpleNamespace(control=control), prepare)
    delivered = []
    agent = SimpleNamespace(redirect=lambda text: delivered.append(text) or True)
    steering.listen(agent)
    try:
        assert entered.wait(1)
        with ThreadPoolExecutor() as pool:
            def progress():
                with steering.model_wait() as generation:
                    assert not steering.cancelled(generation)
                    assert steering.receipts() == []
                steering.step(agent)
                steering.close()
            pool.submit(progress).result(timeout=0.5)
    finally:
        release.set()
        steering.close()
        steering.thread.join(timeout=2)
    assert not steering.thread.is_alive() and not delivered


@pytest.mark.parametrize('supported', [False, True])
def test_immediate_receipt_requires_server_capability(supported):
    calls = []
    def control(body):
        calls.append(body)
        return {'input': {'id': 1, 'content': 'Correction'}, 'receipt_only_supported': supported}
    steering = ActiveTurnSteering(SimpleNamespace(control=control))
    delivered = []
    steering._poll(SimpleNamespace(redirect=lambda text: delivered.append(text) or True), boundary=False)
    assert len(delivered) == 1
    assert calls[0] == {'version': 2, 'applied': []}
    assert len(calls) == (2 if supported else 1)
    if supported:
        assert calls[1] == {'version': 2, 'applied': [1], 'receipt_only': True}


def test_rejected_redirect_is_retained_and_boundary_drains_without_network():
    prepared, delivered, calls = [], [], []
    def control(body):
        calls.append(body)
        return {'input': {'id': 1, 'content': 'Correction'}}
    steering = ActiveTurnSteering(SimpleNamespace(control=control), prepared.append)
    agent = SimpleNamespace(redirect=lambda text: False, steer=lambda text: delivered.append(text) or True)
    steering._poll(agent, boundary=False)
    assert steering.receipts() == [] and len(prepared) == 1
    with steering.poll_lock:
        steering.step(agent)
    assert steering.receipts() == [1] and len(delivered) == 1 and len(calls) == 1
    steering._poll(agent, boundary=False)
    assert len(prepared) == 1 and len(delivered) == 1


def test_cross_scope_monitor_never_globally_interrupts_and_reclassifies_at_boundary():
    packet = {'steer_message_id': 7}
    calls = []
    steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: packet))
    model_active = threading.Event()
    model_active.set()
    agent = SimpleNamespace(_model_request_active=model_active,
                            interrupt=lambda: calls.append('interrupt'),
                            steer=lambda text: calls.append(text) or True)
    steering._poll(agent, boundary=False)
    assert not steering.requested and steering.message_id is None and calls == []
    # A completed model_switch makes this eligible for native delivery.
    packet = {'input': {'id': 7, 'content': 'Same model now'}}
    steering.step(agent)
    assert not steering.requested and steering.receipts() == [7]
    assert 'interrupt' not in calls


def test_cross_scope_still_interrupts_at_safe_boundary():
    calls = []
    steering = ActiveTurnSteering(SimpleNamespace(control=lambda body: {'steer_message_id': 7}))
    steering.step(SimpleNamespace(interrupt=lambda: calls.append('interrupt')))
    assert steering.requested and steering.message_id == 7 and calls == ['interrupt']


def setup_control(app, run_id):
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run_id,))
    app.state.store.update_run(run_id, token_hash=digest('test-control'))
    return {'Authorization': 'Bearer test-control'}


def test_receipt_only_does_not_claim_next_input(queue):
    app, client, run_id, first, target, user = queue
    headers = setup_control(app, run_id)
    assert change(client, run_id, target, 'steer').status_code == 200
    route = f'/broker/{run_id}/control'
    packet = client.post(route, headers=headers, json={'version': 2}).json()
    assert packet['input']['id'] == target and packet['receipt_only_supported']
    body = {'version': 2, 'applied': [target], 'receipt_only': True}
    assert client.post(route, headers=headers, json=body).status_code == 200
    next_message, _ = app.state.store.enqueue_message(run_id, 'Next correction', 'next-correction',
                                                     user_id=user, send_now=True)
    assert client.post(route, headers=headers, json=body).json()['steer_message_id'] is None
    row = app.state.store.rows('SELECT * FROM messages WHERE id=?', (next_message['id'],))[0]
    assert not row['queue_locked'] and row['status'] == 'queued'
    assert app.state.store.messages(run_id)[1]['status'] == 'injected'


@pytest.mark.parametrize('owned_inbox', [False, True])
def test_native_monitor_delivers_and_clears_actual_queue_promptly(queue, owned_inbox):
    app, client, run_id, first, target, user = queue
    headers = setup_control(app, run_id)
    first_poll, acknowledged = threading.Event(), threading.Event()
    def control(body):
        response = client.post(f'/broker/{run_id}/control', headers=headers, json=body)
        assert response.status_code == 200
        if body.get('receipt_only'):
            acknowledged.set()
        first_poll.set()
        return response.json()
    delivered = []
    steering = ActiveTurnSteering(SimpleNamespace(control=control))
    agent = SimpleNamespace(redirect=lambda text: delivered.append(text) or True)
    if owned_inbox:
        agent.inputs = HarnessInputs(TurnJournal([], 'Original request'))
        agent.accept_input = agent.inputs.accept
    steering.listen(agent)
    try:
        assert first_poll.wait(2)
        started = time.monotonic()
        assert change(client, run_id, target, 'steer').status_code == 200
        assert acknowledged.wait(0.8)
        elapsed = time.monotonic() - started
        assert app.state.store.messages(run_id)[1]['status'] == 'injected'
        if owned_inbox:
            assert agent.inputs.take() == ['[User correction to the current task]\nQueued request']
        else:
            assert len(delivered) == 1
        print(f'Send now through runtime callback and durable receipt: {elapsed:.3f}s')
    finally:
        steering.close()
        steering.thread.join(timeout=2)
