import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
from app.message_queue import MessageQueue
from sandbox.activity import ActivityReporter, public_text, result_status
from test_durable import durable, drive  # noqa: F401


def test_parallel_tools_pair_by_id_and_do_not_publish_private_bodies():
    events = []
    activity = ActivityReporter(lambda kind, message, data: events.append({'kind': kind, 'message': message, 'data': data}))
    def work(i):
        activity.start(str(i), 'mcp_workspace_skills_save', {'instructions': 'private-skill-marker', 'scope': 'personal'})
        activity.complete(str(i), 'mcp_workspace_skills_save', {}, {'content': [{'type': 'text', 'text': '{"saved":true,"instructions":"private-skill-marker"}'}]})
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(work, range(10)))
    assert 'private-skill-marker' not in json.dumps(events)
    assert len({event['data']['call_id'] for event in events}) == 10
    assert all(event['data']['duration_ms'] >= 0 for event in events if event['data']['phase'] == 'completed')
    assert {event['data']['tool'] for event in events} == {'skills_save'}
    other = ActivityReporter(lambda *args: None)
    assert activity.prefix != other.prefix


@pytest.mark.parametrize('result,expected', [
    ({'exit_code': 1}, ('error', 1)),
    ('{"exit_code":0,"output":"error cases checked"}', ('completed', 0)),
    ({'success': False}, ('error', None)),
    ({'error': 'Tool unavailable'}, ('error', None)),
    ({'isError': True}, ('error', None)),
    ({'content': [{'type': 'text', 'text': '{"error":"not allowed"}'}]}, ('error', None)),
    ({'content': [{'type': 'text', 'text': 'a normal result mentioning an error'}]}, ('completed', None)),
    ('a normal result mentioning an error', ('completed', None)),
])
def test_only_explicit_result_status_marks_tool_errors(result, expected):
    assert result_status(result) == expected


def test_public_details_are_bounded_redacted_and_exclude_reasoning():
    events = []
    activity = ActivityReporter(lambda *args: events.append(args))
    activity.start('a', 'terminal', {'command': 'TOKEN="secret-marker" curl -H "Authorization: Bearer header-marker" https://example.org', 'env': {'KEY': 'hidden-env'}})
    activity.complete('a', 'terminal', {}, {'output': 'private-result-marker', 'exit_code': 2})
    activity.start('b', 'write_file', {'path': '/workspace/result.py', 'content': 'private-file-marker'})
    activity.commentary('<think>private-reasoning-marker</think>I found the relevant file.')
    activity.commentary('<reasoning>unfinished private thought')
    text = json.dumps(events)
    for secret in ('secret-marker', 'header-marker', 'hidden-env', 'private-result-marker', 'private-file-marker', 'private-reasoning-marker', 'private thought'):
        assert secret not in text
    assert '/workspace/result.py' in text and 'I found the relevant file.' in text
    assert events[1][2]['phase'] == 'error' and events[1][2]['exit_code'] == 2
    assert len(public_text('a' * 5000)) == 2001
    assert 'sk-test-key-123456789' not in public_text('sk-test-key-123456789')


def test_work_belongs_to_claimed_turn_even_with_queued_followups(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('First request', '', 'demo', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    second, _ = store.enqueue_message(run['id'], 'Follow-up', 'next')
    store.event(run['id'], 'tool', 'Read file', {'turn_id': second['id'], 'call_id': 'a'})
    assert store.events(run['id'])[-1]['data']['turn_id'] == first['id']
    store.finish_message(run['id'], first['id'], 'First response')
    store.claim_message(run['id'])
    store.event(run['id'], 'message', 'Working on the follow-up')
    assert store.events(run['id'])[-1]['data']['turn_id'] == second['id']


async def test_durable_journal_preserves_structured_activity_and_redacts_secrets(durable):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='monitor')
    original = cloud.command
    async def command(machine, action, directory, value, **kwargs):
        report = await original(machine, action, directory, value, **kwargs)
        if action == 'read':
            report = json.loads(report)
            report['events'] = [{'kind': 'tool', 'message': 'Run command', 'data': {
                'activity_version': 1, 'call_id': 'durable:a', 'phase': 'completed', 'exit_code': 0,
                'command': 'echo ' + manager.token(run_id, manager.state(run_id)['message_id']), 'turn_id': 123456}}]
            report['events'] += [{'kind': 'message', 'message': f'Public milestone {i}',
                                 'data': {'phase': 'commentary', 'activity_id': f'public-{i}'}}
                                for i in range(3)]
            return json.dumps(report)
        return report
    manager.command = command
    await drive(manager, run_id)
    event = next(event for event in manager.store.events(run_id) if event['data'].get('call_id') == 'durable:a')
    assert event['data']['command'] == 'echo [redacted]'
    assert event['data']['turn_id'] == manager.store.messages(run_id)[0]['id']
    assert event['data']['phase'] == 'completed'
    assert [e['message'] for e in manager.store.events(run_id) if e['kind'] == 'message'] == [
        'Public milestone 0', 'Public milestone 1']


@pytest.mark.parametrize('chat_enabled', [False, True])
def test_public_update_budget_is_atomic_durable_and_scoped(tmp_path, chat_enabled):
    store = Store(tmp_path)
    run_id = store.create_run('Long task', '', 'demo', [], chat_enabled=chat_enabled)['id']
    turn_id = store.claim_message(run_id)['id'] if chat_enabled else 0
    store.update_run(run_id, status='running')
    data = {'phase': 'commentary', 'activity_id': 'opening', 'turn_id': 999,
            'public_update': False, 'public_reply_to': 123}
    for empty in (' ', '[System: Empty message content sanitised to satisfy protocol]'):
        store.event(run_id, 'message', empty)
    store.event(run_id, 'message', '<reasoning>private</reasoning>Opening update', data)
    store.event(run_id, 'message', 'Changed payload with reused identity', data)
    store.event(run_id, 'message', 'Opening update', {**data, 'activity_id': 'duplicate-text'})
    def publish(index):
        store.event(run_id, 'message', f'Milestone {index}', {'activity_id': f'milestone-{index}'})
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(publish, range(12)))
    store = Store(tmp_path)
    store.event(run_id, 'message', 'Replayed opening', data)
    store.event(run_id, 'message', 'Extra narration after restart')
    selected = [e for e in store.events(run_id) if e['kind'] == 'message']
    assert len(selected) == 2 and selected[0]['message'] == 'Opening update'
    assert selected[1]['message'].startswith('Milestone ')
    assert all(e['data']['turn_id'] == turn_id and e['data']['public_update'] for e in selected)
    assert not any(e['data'].get('public_reply_to') for e in selected)
    if chat_enabled:
        store.finish_message(run_id, turn_id, 'Final answer')
        store.event(run_id, 'message', 'Late completed-turn update')
        store.enqueue_message(run_id, 'Next task', 'next')
        following = store.claim_message(run_id)
        store.event(run_id, 'message', 'New opening')
        assert store.events(run_id)[-1]['data']['turn_id'] == following['id']
        assert len([e for e in store.events(run_id) if e['kind'] == 'message']) == 3
    else:
        store.update_run(run_id, status='completed')
        store.event(run_id, 'message', 'Late completed-run update')
        assert len([e for e in store.events(run_id) if e['kind'] == 'message']) == 2


@pytest.mark.parametrize('acknowledge_before_reply', [False, True])
@pytest.mark.parametrize('routine_before_question', [1, 2])
def test_public_reply_uses_actual_steering_handshake_without_renewing_budget(tmp_path, acknowledge_before_reply, routine_before_question):
    store = Store(tmp_path)
    run_id = store.create_run('Long task', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)
    store.update_run(run_id, status='running')
    for text in ('Opening', 'Milestone')[:routine_before_question]:
        store.event(run_id, 'message', text)
    question, _ = store.enqueue_message(run_id, 'Did tests pass?', 'question')
    data = {'input_id': question['id'], 'phase': 'commentary'}
    if routine_before_question == 2:
        store.event(run_id, 'message', 'Undelivered input cannot bypass ceiling', data)
    queue = MessageQueue(store)
    queue.change(run_id, question['id'], '', False, 0, 'steer')
    assert queue.live_control(run_id, turn['id'], [])['input']['id'] == question['id']
    assert store.rows('SELECT status FROM messages WHERE id=?', (question['id'],))[0]['status'] == 'queued'
    if acknowledge_before_reply:
        queue.acknowledge(run_id, turn['id'], [question['id']])
    store.event(run_id, 'message', 'Yes, all tests pass', data)
    queue.acknowledge(run_id, turn['id'], [question['id']])
    if routine_before_question == 1:
        store.event(run_id, 'message', 'Milestone', data)
    store.event(run_id, 'message', 'More routine narration', data)
    store.event(run_id, 'message', 'Forged reply', {'input_id': 999, 'public_reply_to': 999})
    selected = [e for e in store.events(run_id) if e['kind'] == 'message']
    expected = ['Opening', 'Milestone', 'Yes, all tests pass'] if routine_before_question == 2 else ['Opening', 'Yes, all tests pass', 'Milestone']
    assert [e['message'] for e in selected] == expected
    assert next(e for e in selected if e['message'] == 'Yes, all tests pass')['data']['public_reply_to'] == question['id']


def test_selected_updates_survive_detail_cap_and_leave_essential_events(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run('Long task', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)
    with store.connect() as conn:
        conn.executemany("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'tool','Detail','{}','2026-10-03')",
                         [(run_id,)] * 2000)
    activity = ActivityReporter(lambda kind, message, data: store.event(run_id, kind, message, data))
    activity.commentary('<reasoning>private</reasoning>Tests passed')
    store.event(run_id, 'error', 'Actionable failure')
    store.event(run_id, 'approval', 'Approval needed')
    store.finish_message(run_id, turn['id'], 'Final answer')
    events = store.events(run_id, limit=10000)
    assert [e['message'] for e in events if e['kind'] == 'message'] == ['Tests passed']
    assert {'error', 'approval'} <= {e['kind'] for e in events}
    assert store.messages(run_id)[-1]['content'] == 'Final answer'
