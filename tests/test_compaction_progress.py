"""Maintenance remains visible without suppressing task progress or direct replies."""
import pytest
from app.db import Store
from app.message_queue import MessageQueue
from sandbox.activity import ActivityReporter

NOTICES = [
    'Compacting saved context before continuing. Completed tool receipts are preserved.',
    'The agent compacted its context and is continuing. Completed tool receipts remain saved.',
]

@pytest.mark.parametrize('chat', [True, False])
def test_notices_have_a_bounded_durable_allowance(tmp_path, chat):
    store = Store(tmp_path)
    run = store.create_run('Task', '', 'demo', [], chat_enabled=chat)['id']
    if chat:
        store.claim_message(run)
    store.update_run(run, status='running')
    reporter = ActivityReporter(lambda k, t, d: store.event(run, k, t, d))
    reporter.commentary('Opening')
    reporter.commentary(NOTICES[0])
    reporter.commentary('Milestone')
    reporter.commentary(NOTICES[1])
    store = Store(tmp_path)
    for notice in NOTICES:
        store.event(run, 'message', notice)
    # Similar prose and forged metadata must not create an unbounded exemption.
    store.event(run, 'message', 'Compacting the context', {'maintenance': True})
    store.event(run, 'message', 'More narration')
    assert [e['message'] for e in store.events(run) if e['kind'] == 'message'] == [
        'Opening', NOTICES[0], 'Milestone', NOTICES[1]]
    store.update_run(run, status='idle')
    store.event(run, 'message', NOTICES[0])
    assert len([e for e in store.events(run) if e['kind'] == 'message']) == 4


def test_notice_does_not_consume_a_delivered_question_reply(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Task', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run)
    store.update_run(run, status='running')
    for text in ('Opening', 'Milestone'):
        store.event(run, 'message', text)
    question, _ = store.enqueue_message(run, 'Did tests pass?', 'question')
    queue = MessageQueue(store)
    queue.change(run, question['id'], '', False, 0, 'steer')
    assert queue.live_control(run, turn['id'], [])['input']['id'] == question['id']
    data = {'input_id': question['id']}
    store.event(run, 'message', NOTICES[0], data)
    store.event(run, 'message', 'Yes, tests passed', data)
    events = [e for e in store.events(run) if e['kind'] == 'message']
    assert events[-2]['data']['public_reply_to'] is None
    assert events[-1]['message'] == 'Yes, tests passed'
    assert events[-1]['data']['public_reply_to'] == question['id']
