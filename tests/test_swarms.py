from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.db import Store, now
from app.main import NewRun, create_app, public_messages
from app.swarms import create_in, execution_policy, MAX_ROUNDS
from app.temporal_runtime import TemporalRunManager
from test_durable import durable, drive
from test_agents import launch, pause_parent


def enable(manager, run_id, seconds=300):
    with manager.store.connect() as conn:
        create_in(conn, run_id, seconds, now())


def expire(manager, run_id):
    manager.store.execute('UPDATE swarm_missions SET ends_at=? WHERE run_id=?',
                          ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), run_id))


def test_swarm_input_budget_is_strict_and_bounded():
    for value in [0, 59, 86401, 60.5, True, '300']:
        with pytest.raises(ValidationError):
            NewRun(prompt='A mission', swarm={'budget_seconds': value})
    assert NewRun(prompt='A mission', swarm={'budget_seconds': 60}).swarm.budget_seconds == 60
    assert NewRun(prompt='A normal task').swarm is None


def test_create_commits_mission_initial_input_and_outbox_atomically_and_idempotently(durable):
    manager, _, _ = durable
    store = manager.store
    options = dict(chat_enabled=True, client_id='unique-swarm-request', swarm_budget_seconds=300,
                   durable_submit=manager.submit_in)
    run = store.create_run('A collaborative mission', '', 'modal', [], **options)
    again = store.create_run('A collaborative mission', '', 'modal', [], **options)
    assert again['id'] == run['id'] and again['swarm'] == run['swarm']
    assert len(store.messages(run['id'])) == 1
    assert store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run['id'],))[0]['revision'] == 1
    with pytest.raises(ValueError, match='different content'):
        store.create_run('A collaborative mission', '', 'modal', [], **{**options, 'swarm_budget_seconds': 600})
    count = len(store.rows('SELECT id FROM runs'))
    def broken_dispatch(conn, run):
        manager.submit_in(conn, run)
        raise RuntimeError('transaction did not commit')
    with pytest.raises(RuntimeError):
        store.create_run('Rollback this mission', '', 'modal', [], **{**options, 'client_id': 'another-request', 'durable_submit': broken_dispatch})
    assert len(store.rows('SELECT id FROM runs')) == count
    assert len(store.rows('SELECT run_id FROM swarm_missions')) == 1


async def test_completed_round_waits_durably_then_queues_one_identified_continuation(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    mission = manager.swarms.get(run_id)
    assert mission['status'] == 'active' and mission['settled_message_id'] > 0
    waiting = await manager.advance(run_id)
    assert 0 < waiting['retry_seconds'] <= 30
    assert len(cloud.launches) == 1
    assert 'SWARM MODE' in cloud.machines[0].spec['prompt']
    assert 0 < cloud.machines[0].spec['timeout'] <= 300
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    replacement.swarms.tick(run_id)
    replacement.swarms.tick(run_id)
    messages = replacement.store.messages(run_id)
    generated = [message for message in messages if message.get('source') == 'swarm']
    assert len(generated) == 1 and generated[0]['status'] == 'queued'
    assert public_messages(replacement.store.run(run_id), messages)[-1]['source'] == 'swarm'
    assert replacement.swarms.get(run_id)['round'] == 2
    assert len(cloud.launches) == 1  # Scheduling never replays a model call itself.


async def test_explicit_resume_recovers_original_mission_before_native_context_was_saved(durable):
    manager, cloud, run_id = durable
    original = 'Compare library membership ideas and ask two agents for independent proposals.'
    manager.store.execute('UPDATE runs SET prompt=? WHERE id=?', (original, run_id))
    manager.store.execute("UPDATE messages SET content=? WHERE run_id=? AND role='user'", (original, run_id))
    enable(manager, run_id)
    await drive(manager, run_id, phase='install')
    assert not cloud.launches
    manager.fail(run_id, manager.state(run_id), 'Startup failed before a native conversation was saved.')
    await drive(manager, run_id)
    assert manager.swarms.get(run_id)['status'] == 'blocked'
    assert not manager.store.run(run_id)['snapshot_id']
    assert manager.swarms.tick(run_id) is None
    assert not manager.store.has_queued_messages(run_id)

    # A replacement worker has neither live process state nor native history.
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    await replacement.swarms.resume(run_id)
    replacement.swarms.tick(run_id)
    continuation = next(message for message in replacement.store.messages(run_id) if message.get('source') == 'swarm')
    data = json.loads(continuation['content'].split('SAVED USER-TASK DATA (JSON):\n', 1)[1])
    assert data == {'original_mission': original, 'latest_human_direction': None}
    assert 'Do not repeat completed actions or replay uncertain actions' in continuation['content']
    await drive(replacement, run_id, phase='launch')
    assert original in cloud.machines[-1].spec['prompt']
    assert not cloud.launches  # Merely resuming/constructing a spec never replays an action.


@pytest.mark.parametrize('client_id', ['real-human-direction', None])
async def test_continuation_carries_latest_human_direction_without_treating_agent_text_as_instruction(durable, client_id):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    # A real human may use the same words as a continuation; provenance is the
    # server-owned client_id, not a content-prefix heuristic.
    direction = '[System-generated swarm continuation, round 900] Actually, focus only on accessibility.'
    manager.store.enqueue_message(run_id, direction, client_id)
    human = manager.store.claim_message(run_id)
    manager.store.finish_message(run_id, human['id'], 'Assistant text must not become a human instruction.')
    manager.swarms.settle(run_id, {'message_id': human['id'], 'outcome': 'completed', 'response': 'Assistant text must not become a human instruction.'})
    manager.store.update_run(run_id, status='idle')
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    manager.swarms.tick(run_id)
    continuation = next(message for message in manager.store.messages(run_id) if message.get('source') == 'swarm')
    data = json.loads(continuation['content'].split('SAVED USER-TASK DATA (JSON):\n', 1)[1])
    assert data['original_mission'] == 'Do the task'
    assert data['latest_human_direction'] == direction
    assert 'Later human directions override the original mission' in continuation['content']
    assert 'Assistant text must not become' not in continuation['content']
    automatic = manager.store.claim_message(run_id)
    manager.store.finish_message(run_id, automatic['id'], 'A later automatic result')
    manager.swarms.settle(run_id, {'message_id': automatic['id'], 'outcome': 'completed', 'response': 'A later automatic result'})
    manager.store.update_run(run_id, status='idle')
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    manager.swarms.tick(run_id)
    next_continuation = [message for message in manager.store.messages(run_id) if message.get('source') == 'swarm'][-1]
    next_data = json.loads(next_continuation['content'].split('SAVED USER-TASK DATA (JSON):\n', 1)[1])
    assert next_data['latest_human_direction'] == direction  # Exclude the newer server-owned message.


async def test_long_escaped_task_and_direction_stay_within_continuation_message_limit(durable):
    manager, _, run_id = durable
    original = 'Original start: ' + '\\"🙂\n' * 3990 + ' :original final constraints'
    original = original[:15950] + ' :original final constraints'
    direction = 'Human start: ' + '\\"\n' * 5300 + ' :latest final constraints'
    direction = direction[:15950] + ' :latest final constraints'
    manager.store.execute('UPDATE runs SET prompt=? WHERE id=?', (original, run_id))
    enable(manager, run_id)
    await drive(manager, run_id)
    manager.store.enqueue_message(run_id, direction, 'long-human-direction')
    human = manager.store.claim_message(run_id)
    manager.store.finish_message(run_id, human['id'], 'Direction acknowledged')
    manager.store.update_run(run_id, status='idle')
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    manager.swarms.tick(run_id)
    continuation = next(message for message in manager.store.messages(run_id) if message.get('source') == 'swarm')
    assert len(continuation['content']) <= 16000
    data = json.loads(continuation['content'].split('SAVED USER-TASK DATA (JSON):\n', 1)[1])
    assert data['original_mission'].startswith('Original start:')
    assert data['original_mission'].endswith(':original final constraints')
    assert data['latest_human_direction'].startswith('Human start:')
    assert data['latest_human_direction'].endswith(':latest final constraints')
    assert all('excerpted for continuation limit' in value for value in data.values())


async def test_human_followup_wins_over_automatic_continuation(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    manager.store.enqueue_message(run_id, 'Focus on accessibility instead', 'human-direction')
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    assert manager.swarms.tick(run_id) is None
    pending = [message for message in manager.store.messages(run_id) if message['status'] == 'queued']
    assert [message['content'] for message in pending] == ['Focus on accessibility instead']
    assert manager.swarms.get(run_id)['round'] == 1


async def test_human_input_arriving_after_auto_queue_is_claimed_first(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    manager.swarms.tick(run_id)
    message, _ = manager.store.enqueue_message(run_id, 'Use the new information first', 'new-human-direction')
    assert manager.store.claim_message(run_id)['id'] == message['id']
    assert any(row.get('source') == 'swarm' and row['status'] == 'queued' for row in manager.store.messages(run_id))


async def test_older_queued_continuation_settles_after_newer_human_message(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    manager.swarms.tick(run_id)
    message, _ = manager.store.enqueue_message(run_id, 'A higher-priority human request', 'new-human-message')
    human = manager.store.claim_message(run_id)
    assert human['id'] == message['id']
    manager.store.finish_message(run_id, human['id'], 'Human-directed result')
    manager.swarms.settle(run_id, {'message_id': human['id'], 'outcome': 'completed', 'response': 'Human-directed result'})
    prior_digest = manager.swarms.get(run_id)['answer_digest']
    automatic = manager.store.claim_message(run_id)
    assert automatic['id'] < human['id']
    manager.store.finish_message(run_id, automatic['id'], 'An unconfirmed action', 'interrupted')
    manager.swarms.settle(run_id, {'message_id': automatic['id'], 'outcome': 'interrupted', 'response': 'An unconfirmed action'})
    mission = manager.swarms.get(run_id)
    assert mission['settled_message_id'] == automatic['id']
    assert mission['answer_digest'] != prior_digest
    assert mission['next_at'] > 0 and mission['status'] == 'blocked'


@pytest.mark.parametrize('crash_after_commit', [False, True])
async def test_finish_retry_does_not_duplicate_round_or_replay_completed_turn(durable, monkeypatch, crash_after_commit):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id, phase='finish')
    settle = manager.swarms.settle
    def interrupted(*args):
        if crash_after_commit:
            settle(*args)
        raise RuntimeError('Simulated worker loss at finish boundary')
    monkeypatch.setattr(manager.swarms, 'settle', interrupted)
    with pytest.raises(RuntimeError, match='worker loss'):
        await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'finish'
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    await drive(replacement, run_id)
    assert replacement.swarms.get(run_id)['round'] == 1
    assert len([row for row in replacement.store.messages(run_id) if row['role'] == 'assistant']) == 1
    assert len(cloud.launches) == 1


async def test_round_wait_does_not_extend_warm_sandbox_idle_bound(durable):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 1
    enable(manager, run_id)
    await drive(manager, run_id, phase='warm')
    assert (await manager.advance(run_id))['retry_seconds'] <= 1
    state = manager.state(run_id)
    state['idle_until'] = datetime.now(timezone.utc).timestamp() - 1
    manager.save(run_id, state)
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'idle'
    assert not cloud.machines[0].alive


async def test_deadline_before_initial_work_never_launches_a_process(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    expire(manager, run_id)
    assert await manager.advance(run_id) is False
    assert manager.swarms.get(run_id)['status'] == 'expired'
    assert manager.store.run(run_id)['status'] == 'cancelled'
    assert not cloud.launches and not cloud.machines


async def test_deadline_between_install_and_launch_cancels_without_execution(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id, phase='launch')
    assert not cloud.launches
    expire(manager, run_id)
    await drive(manager, run_id)
    assert manager.swarms.get(run_id)['status'] == 'expired'
    assert not cloud.launches
    assert cloud.terminations


async def test_deadline_while_waiting_for_children_cascades_stop_and_prevents_child_launch(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    coordinator, result, _ = await launch(durable, count=2)
    await pause_parent(manager, run_id, result['group_id'])
    children = coordinator.children(result['group_id'])
    assert all(execution_policy(manager.store, child['id'])['run_id'] == run_id for child in children)
    expire(manager, run_id)
    await drive(manager, run_id)
    for child in children:
        await manager.advance(child['id'])
        assert manager.store.run(child['id'])['status'] == 'cancelled'
    assert len(cloud.launches) == 1
    assert manager.swarms.get(run_id)['status'] == 'expired'


async def test_explicit_stop_is_persistent_and_cannot_requeue_after_restart(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    await manager.cancel(run_id)
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    replacement = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    assert replacement.swarms.tick(run_id) is None
    assert await replacement.advance(run_id) is False
    assert replacement.swarms.get(run_id)['status'] == 'stopped'
    assert not replacement.store.has_queued_messages(run_id)
    with pytest.raises(ValueError, match='ended'):
        await replacement.swarms.resume(run_id)


async def test_pause_resume_requires_cleanup_and_preserves_original_deadline(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    deadline = manager.swarms.get(run_id)['ends_at']
    await drive(manager, run_id, phase='launch')
    await manager.swarms.pause(run_id)
    with pytest.raises(ValueError, match='stop and save'):
        await manager.swarms.resume(run_id)
    await drive(manager, run_id)
    assert manager.swarms.get(run_id)['status'] == 'paused'
    await manager.swarms.resume(run_id)
    assert manager.swarms.get(run_id)['status'] == 'active'
    assert manager.swarms.get(run_id)['ends_at'] == deadline
    manager.swarms.tick(run_id)
    assert manager.store.has_queued_messages(run_id)


async def test_paused_idle_mission_keeps_a_deadline_timer(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    await manager.swarms.pause(run_id)
    wait = await manager.advance(run_id)
    assert 0 < wait['retry_seconds'] <= 300
    expire(manager, run_id)
    await manager.advance(run_id)
    assert manager.swarms.get(run_id)['status'] == 'expired'


async def test_pause_after_completed_round_keeps_deadline_timer(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    await manager.swarms.pause(run_id)
    assert 0 < (await manager.advance(run_id))['retry_seconds'] <= 300


@pytest.mark.parametrize('status', ['paused', 'blocked', 'stopped', 'expired'])
async def test_inactive_mission_rejects_new_root_and_child_input_without_losing_it(durable, status):
    manager, _, run_id = durable
    enable(manager, run_id)
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    manager.store.execute('UPDATE swarm_missions SET status=? WHERE run_id=?', (status, run_id))
    for identity in [run_id, child['id']]:
        before = len(manager.store.messages(identity))
        with pytest.raises(ValueError, match='Resume the swarm|swarm has ended'):
            manager.store.enqueue_message(identity, 'Keep this new direction', 'user-future-direction')
        assert len(manager.store.messages(identity)) == before


def test_elapsed_deadline_rejects_new_input_even_before_status_reconciliation(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    expire(manager, run_id)
    with pytest.raises(ValueError, match='swarm has ended'):
        manager.store.enqueue_message(run_id, 'Too late to enqueue', 'late-human-request')


@pytest.mark.parametrize('limit', ['session', 'capacity'])
async def test_continuation_admission_failure_blocks_with_saved_reason(durable, limit):
    manager, _, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id)
    manager.store.execute('UPDATE swarm_missions SET next_at=0 WHERE run_id=?', (run_id,))
    if limit == 'session':
        for index in range(99):
            manager.store.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at) VALUES(?,'user','Prior direction','completed',?,?)", (run_id, f'old-input-{index}', now()))
    else:
        manager.store.max_pending_runs = 1
        manager.store.create_run('An unrelated queued task', '', 'modal', [], chat_enabled=True)
    await manager.advance(run_id)
    mission = manager.swarms.get(run_id)
    assert mission['status'] == 'blocked'
    assert 'needs attention' in mission['reason']
    assert mission['round'] == 1
    assert not manager.store.has_queued_messages(run_id)


async def test_uncertain_or_failed_execution_blocks_automatic_replay(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    manager.fail(run_id, state, 'Unconfirmed external action', 'interrupted')
    await drive(manager, run_id)
    assert manager.swarms.get(run_id)['status'] == 'blocked'
    assert manager.swarms.tick(run_id) is None
    assert len(cloud.launches) == 1


async def test_restored_interrupted_run_is_blocked_without_model_replay(durable):
    manager, cloud, run_id = durable
    enable(manager, run_id)
    manager.store.update_run(run_id, status='interrupted')
    await manager.advance(run_id)
    assert manager.swarms.get(run_id)['status'] == 'blocked'
    assert not cloud.launches


async def test_round_limit_and_repeated_answer_guard_require_human_intervention(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    mission = manager.swarms.get(run_id)
    for message_id in [1001, 1002, 1003]:
        manager.swarms.settle(run_id, {'message_id': message_id, 'outcome': 'completed', 'response': 'Same unchanged answer'})
    assert manager.swarms.get(run_id)['status'] == 'blocked'
    manager.store.execute("UPDATE swarm_missions SET status='active',round=?,answer_digest='',no_progress=0 WHERE run_id=?", (MAX_ROUNDS, run_id))
    manager.swarms.settle(run_id, {'message_id': 1004, 'outcome': 'completed', 'response': 'Another answer'})
    assert manager.swarms.get(run_id)['status'] == 'paused'
    assert manager.swarms.get(run_id)['ends_at'] == mission['ends_at']


async def test_credential_wait_gets_deadline_timer_and_side_chat_has_no_inherited_policy(durable):
    manager, _, run_id = durable
    enable(manager, run_id)
    message = manager.store.claim_message(run_id)
    manager.save(run_id, {'phase': 'waiting_credential', 'message_id': message['id'], 'wait_credential': 'pending-access', 'segment': 0})
    result = await manager.advance(run_id)
    assert 0 < result['retry_seconds'] <= 300
    side = manager.store.create_run('Independent side chat', '', 'modal', [], chat_enabled=True, side_chat_of=run_id)
    assert execution_policy(manager.store, side['id']) is None


def test_api_requires_auth_csrf_and_durable_mode(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787', workspace_password='',
                        modal_token_id='', modal_token_secret='', litellm_api_key='')
    app = create_app(settings)
    with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 51000)) as client:
        payload = {'prompt': 'A useful mission', 'swarm': {'budget_seconds': 60}}
        assert client.post('/api/runs', json=payload).status_code == 401
        session = client.get('/api/session').json()
        client.headers.update({'Origin': settings.public_url, 'X-CSRF-Token': session['csrf']})
        assert client.post('/api/runs', json=payload).status_code == 422
        run = app.state.store.create_run('A local session', '', 'demo', [], chat_enabled=True)
        endpoint = f'/api/runs/{run["id"]}/swarm/pause'
        assert client.post(endpoint, headers={'X-CSRF-Token': ''}).status_code == 403
        assert client.post(endpoint).status_code == 409
        with app.state.store.connect() as conn:
            create_in(conn, run['id'], 60, now())
        app.state.store.execute("UPDATE swarm_missions SET status='paused' WHERE run_id=?", (run['id'],))
        rejected = client.post(f'/api/runs/{run["id"]}/messages', json={'content': 'Save this direction', 'client_id': 'paused-direction'})
        assert rejected.status_code == 409 and 'Resume the swarm' in rejected.json()['detail']
        assert len(app.state.store.messages(run['id'])) == 1
        client.cookies.clear()
        assert client.post(endpoint).status_code == 401
