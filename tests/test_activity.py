import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
from app.message_queue import MessageQueue
from app.progress import active_input, current_focus
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


def test_connector_details_are_bounded_and_private_payloads_stay_omitted():
    events = []
    activity = ActivityReporter(lambda kind, message, data: events.append(data))
    activity.start('repo', 'mcp__moyai__github_repository', {'repository_id': 123})
    activity.complete('repo', 'mcp__moyai__github_repository', {}, {'name': 'moyai', 'token': 'hidden'})
    assert '123' in events[0]['input'] and 'moyai' in events[1]['output']
    assert 'hidden' not in events[1]['output']
    for name in ('credentials_run', 'memory_search', 'skills_read_file'):
        activity.start(name, 'mcp__moyai__' + name, {'value': 'private-marker'})
        activity.complete(name, 'mcp__moyai__' + name, {}, {'value': 'private-marker'})
        assert 'input' not in events[-2] and 'output' not in events[-1]
        assert events[-1]['details_notice']
    assert 'private-marker' not in json.dumps(events)
    activity.start('fill', 'mcp__moyai__browser_fill', {'label': 'Sign in', 'value': 'secret-login'})
    assert 'secret-login' not in events[-1]['input']


def test_view_image_keeps_a_snapshot_and_rejects_outside_paths(tmp_path, monkeypatch):
    from PIL import Image
    from sandbox import tool_images
    monkeypatch.setattr(tool_images, 'WORKSPACE', tmp_path)
    source = tmp_path / 'image.png'
    Image.new('RGB', (4, 4), 'red').save(source)
    events = []
    activity = ActivityReporter(lambda kind, message, data: events.append(data))
    activity.start('image', 'view_image', {'path': str(source)})
    from pathlib import Path
    saved = Path(events[0]['image_path'])
    assert events[0]['image_preview'].startswith('data:image/jpeg;base64,')
    assert len(events[0]['image_preview']) <= 66000
    before = saved.read_bytes()
    Image.new('RGB', (4, 4), 'blue').save(source)
    activity.complete('image', 'view_image', {}, 'Image viewed.')
    assert saved.read_bytes() == before
    assert events[-1]['image_path'] == str(saved)
    assert events[-1]['path'] == str(source)
    assert 'image_path' not in tool_images.snapshot('/etc/passwd')
    linked = tmp_path / 'link.png'
    linked.symlink_to(source)
    assert 'image_path' not in tool_images.snapshot(str(linked))
    assert 'image_path' not in tool_images.snapshot(None)


def test_public_details_are_bounded_redacted_and_exclude_reasoning():
    events = []
    activity = ActivityReporter(lambda *args: events.append(args))
    activity.start('a', 'terminal', {'command': 'TOKEN="secret-marker" curl -H "Authorization: Bearer header-marker" https://example.org', 'env': {'KEY': 'hidden-env'}})
    activity.complete('a', 'terminal', {}, {'output': 'command-result-marker', 'exit_code': 2})
    activity.start('b', 'write_file', {'path': '/workspace/result.py', 'content': 'file-content-marker'})
    activity.commentary('<think>private-reasoning-marker</think>I found the relevant file.')
    activity.commentary('<reasoning>unfinished private thought')
    text = json.dumps(events)
    for secret in ('secret-marker', 'header-marker', 'private-reasoning-marker', 'private thought'):
        assert secret not in text
    assert 'command-result-marker' in text and 'file-content-marker' in text
    assert 'hidden-env' not in text
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
            report['events'].append({'kind': 'status', 'message': 'Verifying the fix', 'data': {
                'phase': 'focus', 'activity_id': 'durable-focus',
                'input_id': manager.state(run_id)['message_id']}})
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
    assert [(e['message'], e['data']['input_id']) for e in manager.store.events(run_id)
            if e['data'].get('live_status')] == [('Verifying the fix', event['data']['turn_id'])]


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


def test_focus_replaces_status_without_spending_chat_budget_and_replay_cannot_rewind(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run('Audit schema and UI changes', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)
    store.update_run(run_id, status='running')
    events = []
    def emit(kind, text, data):
        data = {**data, 'activity_id': str(len(events)), 'input_id': turn['id']}
        events.append((kind, text, data))
        store.event(run_id, kind, text, data)
    reporter = ActivityReporter(emit)
    for text in ('Auditing UI changes', 'Auditing UI changes', 'Checking schema changes', 'Verifying the fix'):
        reporter.commentary(f'<status>{text}</status>')
    reporter.commentary('Opening update')
    reporter.commentary('One useful milestone')
    reporter.commentary('Extra narration')
    store = Store(tmp_path)
    for event in events[:2]:
        store.event(run_id, *event)
    with store.connect() as conn:
        assert current_focus(conn, run_id) == 'Verifying the fix'
    saved = store.events(run_id)
    assert len([e for e in saved if e['data'].get('live_status')]) == 4
    assert [e['message'] for e in saved if e['kind'] == 'message'] == ['Opening update', 'One useful milestone']
    assert all(e['data']['turn_id'] == turn['id'] for e in saved if e['data'].get('live_status'))
    store.finish_message(run_id, turn['id'], 'Done')
    store.event(run_id, 'status', 'Late update', {'phase': 'focus', 'activity_id': 'late', 'input_id': turn['id']})
    following, _ = store.enqueue_message(run_id, 'New task', 'next-focus')
    store.claim_message(run_id)
    with store.connect() as conn:
        assert current_focus(conn, run_id) == ''
    store.event(run_id, 'status', 'Old turn', {'phase': 'focus', 'activity_id': 'old', 'input_id': turn['id']})
    assert not any(e['message'] in {'Late update', 'Old turn'} for e in store.events(run_id))


@pytest.mark.parametrize('text', ['<think>private</think>', 'curl --header secret', 'Read /workspace/private.py',
                                  'https://example.org', '`command`', 'API_KEY=secret-marker', '<b>markup</b>'])
def test_focus_rejects_private_or_raw_details_at_callback_and_store(tmp_path, text):
    emitted = []
    ActivityReporter(lambda *event: emitted.append(event)).commentary(f'<status>{text}</status>')
    assert not emitted
    store = Store(tmp_path)
    run_id = store.create_run('Task', '', 'demo', [], chat_enabled=False)['id']
    store.update_run(run_id, status='running')
    store.event(run_id, 'status', text, {'phase': 'focus', 'activity_id': 'raw'})
    assert not any(e['data'].get('live_status') for e in store.events(run_id))


def test_focus_follows_actual_steering_order_including_unacknowledged_input(tmp_path):
    store = Store(tmp_path)
    run_id = store.create_run('Task', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)
    store.update_run(run_id, status='running')
    older, _ = store.enqueue_message(run_id, 'Older queued input', 'older')
    newer, _ = store.enqueue_message(run_id, 'Newer queued input', 'newer')
    queue = MessageQueue(store)
    def focus(input_id, label):
        store.event(run_id, 'status', label, {'phase': 'focus', 'activity_id': label, 'input_id': input_id, 'turn_id': 999})
    focus(turn['id'], 'Original task')
    for message, label in ((newer, 'Newer input'), (older, 'Older input delivered last')):
        queue.change(run_id, message['id'], '', False, 0, 'steer')
        assert queue.live_control(run_id, turn['id'], [])['input']['id'] == message['id']
        with store.connect() as conn:
            assert current_focus(conn, run_id) == ''
        focus(message['id'], label)
        queue.acknowledge(run_id, turn['id'], [message['id']])
        with store.connect() as conn:
            assert active_input(conn, run_id, turn['id']) == message['id']
            assert current_focus(conn, run_id) == label
    store.event(run_id, 'status', 'Saving before switching requester or model.',
                {'phase': 'steering', 'message_id': newer['id']})
    assert 'message_id' not in store.events(run_id)[-1]['data']
    focus(newer['id'], 'Late newer input')
    with store.connect() as conn:
        assert current_focus(conn, run_id) == 'Older input delivered last'
    for state in ('stopping', 'cancelled', 'completed', 'failed', 'interrupted', 'idle'):
        store.update_run(run_id, status=state)
        focus(older['id'], 'Late ' + state)
        with store.connect() as conn:
            assert current_focus(conn, run_id) == ''
    assert not any(e['message'].startswith('Late') for e in store.events(run_id))


def test_focus_envelope_is_bounded_and_never_becomes_a_chat_update():
    events = []
    reporter = ActivityReporter(lambda *event: events.append(event))
    reporter.commentary('<status>' + 'Reviewing changes ' * 20 + '</status>\nA useful milestone')
    assert len(events[0][1]) == 120 and events[0][0] == 'status'
    assert events[1] == ('message', 'A useful milestone', {'activity_version': 1, 'phase': 'commentary'})
    reporter.commentary('<status>Incomplete envelope')
    reporter.commentary('<status>API_KEY=secret-marker</status>Visible message')
    assert events[-1][1] == 'Visible message'
    assert '<status>' not in json.dumps(events) and 'secret-marker' not in json.dumps(events)


def test_unknown_native_fields_and_messages_do_not_enter_diagnostics():
    from types import SimpleNamespace
    from sandbox.sdk_failure import exception_details, codex_details, claude_details
    assert codex_details({'codexErrorInfo': {'private-payload': {'httpStatusCode': 'secret'}},
                          'message': 'secret'}) == {'source': 'native_error'}
    assert claude_details(SimpleNamespace(subtype='private-subtype', is_error=True,
        terminal_reason='private-reason', errors=['secret'], api_error_status=True)) == {
            'source': 'native_result', 'native_status': 'unknown', 'is_error': True, 'error_count': 1}
    cause = ConnectionResetError(54, 'private-network-body')
    error = RuntimeError('private-exception-body')
    error.__cause__ = cause
    assert exception_details(error) == {'source': 'exception', 'exception_type': 'RuntimeError',
                                        'cause_type': 'ConnectionResetError'}


@pytest.mark.parametrize('failure', ['native', 'mcp'])
async def test_claude_hooks_hide_intermediate_failures_and_collapse_completed_progress(tmp_path, failure):
    from pathlib import Path
    import shutil
    import subprocess
    from types import SimpleNamespace
    from sandbox.claude_harness import ClaudeAgent
    from sandbox.harness_agent import TurnJournal

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to exercise the production activity renderer')
    store = Store(tmp_path)
    run = store.create_run('Save my preference', '', 'demo', [],
                           chat_enabled=True, harness='claude-agent-sdk')
    turn = store.claim_message(run['id'])
    store.update_run(run['id'], status='running')
    emitted = []

    def emit(kind, text, data):
        data = {**data, 'activity_id': str(len(emitted)), 'input_id': turn['id']}
        emitted.append((kind, text, data))
        store.event(run['id'], kind, text, data)

    agent = ClaudeAgent(spec={}, relay=SimpleNamespace(), config={},
                        activity=ActivityReporter(emit), step=lambda: None,
                        cwd=str(tmp_path), definition=None)
    agent.journal = TurnJournal([], 'Save my preference')
    tool = {'tool_name': 'mcp__moyai__memory_save',
            'tool_input': {'content': 'private-preference-marker'}}
    agent.pending_text.append('<status>Saving your preference</status>I will save that preference once.')
    await agent.tool_hook({**tool, 'hook_event_name': 'PreToolUse'}, 'rejected', {})
    error = ({'hook_event_name': 'PostToolUseFailure', 'error': 'temporary-unavailable-marker'}
             if failure == 'native' else {'hook_event_name': 'PostToolUse', 'tool_response': {
                 'isError': True, 'content': [{'type': 'text', 'text': 'temporary-unavailable-marker'}]}})
    await agent.tool_hook({**tool, **error}, 'rejected', {})
    # Journal re-delivery retains source identity; it cannot repeat the opening.
    for kind, text, data in emitted[:2]:
        store.event(run['id'], kind, 'Replayed acknowledgement' if kind == 'message' else text, data)
    agent.pending_text.append('<status>Verifying the saved preference</status>')
    await agent.tool_hook({**tool, 'hook_event_name': 'PreToolUse'}, 'saved', {})
    await agent.tool_hook({**tool, 'hook_event_name': 'PostToolUse',
                          'tool_response': {'saved': True, 'content': 'private-preference-marker'}}, 'saved', {})
    assert not agent.journal.pending
    assert 'temporary-unavailable-marker' in json.dumps(agent.journal.messages)
    events = store.events(run['id'])
    assert [event['data']['phase'] for event in events if event['kind'] == 'tool'] == [
        'started', 'error', 'started', 'completed']
    assert 'private-preference-marker' not in json.dumps(events)
    assert [event['message'] for event in events if event['kind'] == 'message'] == [
        'I will save that preference once.']

    def projection():
        snapshot = {**store.run(run['id']), 'messages': store.messages(run['id']),
                    'events': store.events(run['id'])}
        result = subprocess.run([node, '-e',
            "const fs=require('node:fs'),ui=require(process.argv[1]),run=JSON.parse(fs.readFileSync(0,'utf8'));"
            "const turn=ui.current(run);process.stdout.write(JSON.stringify({"
            "rows:turn.rows.filter(r=>r.kind==='tool').map(r=>r.state),headline:turn.headline,"
            "html:ui.html(turn),collapsed:[...ui.completedHistory(run).keys()]}));",
            str(Path(__file__).resolve().parents[1] / 'app/static/activity.js')],
            input=json.dumps(snapshot), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    active = projection()
    assert active['rows'] == ['completed']
    assert active['headline'] == 'Verifying the saved preference'
    assert active['collapsed'] == []
    assert 'temporary-unavailable-marker' not in active['html']
    store.finish_message(run['id'], turn['id'], 'Saved to your personal preferences.')
    store.update_run(run['id'], status='idle')
    finished = projection()
    assert finished['collapsed'] == [str(turn['id'])]
    assert finished['rows'] == ['completed']
    assert store.messages(run['id'])[-1]['content'] == 'Saved to your personal preferences.'
