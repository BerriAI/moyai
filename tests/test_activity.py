import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
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
            return json.dumps(report)
        return report
    manager.command = command
    await drive(manager, run_id)
    event = next(event for event in manager.store.events(run_id) if event['data'].get('call_id') == 'durable:a')
    assert event['data']['command'] == 'echo [redacted]'
    assert event['data']['turn_id'] == manager.store.messages(run_id)[0]['id']
    assert event['data']['phase'] == 'completed'

