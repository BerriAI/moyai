import asyncio
from contextlib import asynccontextmanager
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agents import AgentCoordinator, Artifact, Fanout, Retry
from app.config import Settings
from app.db import Store
from app.spend import Spend
from app.temporal_runtime import TemporalRunManager
from agent.continuation import AgentWait
from test_durable import durable, drive, transport_failure_report
from test_workspace import workspace
from test_spend import active, sign_in
from storage_fixture import MemoryObjects


def attach(manager):
    manager.settings.temporal_enabled = True
    manager.coordinator = AgentCoordinator(manager.store, manager.settings, manager)
    return manager.coordinator


async def launch(durable, count=5):
    manager, cloud, run_id = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    manager.store.execute("UPDATE messages SET user_id='google:tin' WHERE run_id=?", (run_id,))
    await drive(manager, run_id, phase='monitor')
    args = Fanout(request_key='benchmark-100', instructions='Run the assigned test cases.',
                  items=[str(i) for i in range(1, 101)], workers=count)
    result = await coordinator.fanout(manager.store.run(run_id), args)
    return coordinator, result, args


async def pause_parent(manager, run_id, group_id):
    state = manager.state(run_id)
    result = {'message': 'Waiting for agents', 'continuation': True, 'completed': False, 'wait_group': group_id}
    state.update(phase='save', exit_code=0, result=result)
    manager.store.update_run(run_id, pending_result=json.dumps(result))
    manager.save(run_id, state)
    await drive(manager, run_id, phase='waiting_children')


def test_partition_covers_each_input_once_even_when_values_repeat():
    args = Fanout(request_key='test', instructions='Run each case', items=['same'] * 100, workers=5)
    batches = args.assignments()
    cases = [json.loads(prompt.split('indices):\n')[1].split('\nReturn a result')[0]) for _, prompt in batches]
    assert [len(group) for group in cases] == [20] * 5
    assert [row['index'] for group in cases for row in group] == list(range(1, 101))
    uneven = Fanout(request_key='test', instructions='Run each case', items=['x'] * 103, workers=5).assignments()
    assert [label for label, _ in uneven] == ['Cases 1–21', 'Cases 22–42', 'Cases 43–63', 'Cases 64–83', 'Cases 84–103']
    with pytest.raises(ValidationError):
        Fanout(request_key='test', items=['x'], tasks=[{'label':'a','prompt':'one'}])


async def test_fanout_is_atomic_idempotent_and_inherits_identity_files_model_permissions(durable):
    manager, cloud, run_id = durable
    coordinator, result, args = await launch(durable)
    repeats = await asyncio.gather(*(coordinator.fanout(manager.store.run(run_id), args) for _ in range(3)))
    assert all(row['group_id'] == result['group_id'] for row in repeats)
    children = manager.store.rows('SELECT * FROM runs WHERE parent_run_id=?', (run_id,))
    assert len(children) == 5 and cloud.snapshots == 1
    for child in children:
        assert child['owner_id'] == child['active_user_id'] == 'google:tin'
        assert child['model'] == 'test-model'
        assert child['harness'] == manager.store.run(run_id)['harness']
        assert child['plugins'] == '[]'
        assert child['snapshot_id'] == 'im-1'
        assert manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (child['id'],))[0]['revision'] == 1
        assert manager.store.messages(child['id'])[0]['user_id'] == 'google:tin'
        assert coordinator.tools(manager.store.run(child['id']))
    with pytest.raises(ValueError, match='different work'):
        await coordinator.fanout(manager.store.run(run_id), args.model_copy(update={'workers': 3}))
    with pytest.raises(ValueError, match='enabled'):
        await coordinator.fanout(manager.store.run(run_id), args.model_copy(update={'model': 'unapproved'}))


async def test_fanout_resolves_mixed_runtimes_and_shared_defaults(durable):
    manager, cloud, root = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, root, phase='monitor')
    result = await coordinator.fanout(manager.store.run(root), Fanout(
        request_key='mixed-runtimes', harness='opencode', model='opus', tasks=[
            {'label': 'Research', 'prompt': 'Research the task.', 'harness': 'codex', 'model': 'sol'},
            {'label': 'Review', 'prompt': 'Review the task.'},
            {'label': 'Synthesis', 'prompt': 'Synthesize the task.', 'harness': 'claude-agent-sdk'},
        ]))
    children = {child['agent_label']: child for child in coordinator.children(result['group_id'])}
    expected = {'Research': ('codex', 'openai/gpt-6.1-sol'),
                'Review': ('opencode', 'anthropic/claude-opus-5-5'),
                'Synthesis': ('claude-agent-sdk', 'anthropic/claude-opus-5-5')}
    for label, child in children.items():
        assert (child['harness'], child['model']) == expected[label]
        assert child['active_model'] == child['model']
        assert manager.store.messages(child['id'])[0]['model'] == child['model']
        assert manager.spec(manager.store.run(child['id']))['harness'] == child['harness']
    nodes = coordinator.view(root, include_costs=False)['groups'][0]['children']
    assert {node['agent_label']: (node['harness'], node['model']) for node in nodes} == expected


@pytest.mark.parametrize('invalid', [
    {'harness': 'missing-harness'}, {'model': 'disabled-model'},
    {'harness': 'opencode', 'model': 'astra-ultrafast'},
])
async def test_invalid_second_assignment_rejects_entire_group_before_snapshot(durable, invalid):
    manager, cloud, root = durable
    coordinator = attach(manager)
    await drive(manager, root, phase='monitor')
    before = cloud.snapshots
    with pytest.raises(ValueError):
        await coordinator.fanout(manager.store.run(root), Fanout(request_key='invalid-second', tasks=[
            {'label': 'Valid', 'prompt': 'Do the valid task.', 'harness': 'codex', 'model': 'sol'},
            {'label': 'Invalid', 'prompt': 'Do the invalid task.', **invalid},
        ]))
    assert not manager.store.rows('SELECT * FROM agent_groups WHERE parent_id=?', (root,))
    assert not manager.store.rows('SELECT id FROM runs WHERE parent_run_id=?', (root,))
    assert cloud.snapshots == before


async def test_preparing_group_pins_runtime_identity_across_restart_and_parent_changes(durable, monkeypatch):
    manager, cloud, root = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, root, phase='monitor')
    args = Fanout(request_key='resume-preparing', tasks=[{'label': 'Worker', 'prompt': 'Do the assigned task.'}])
    async def failed_snapshot(run):
        raise RuntimeError('Snapshot temporarily unavailable')
    monkeypatch.setattr(manager, 'snapshot_for_children', failed_snapshot)
    with pytest.raises(RuntimeError, match='Snapshot temporarily'):
        await coordinator.fanout(manager.store.run(root), args)
    group = manager.store.rows('SELECT * FROM agent_groups WHERE parent_id=?', (root,))[0]
    pinned = json.loads(group['runtime_assignments'])[0]
    assert (pinned['harness'], pinned['model']) == ('hermes', 'test-model')
    assert not coordinator.children(group['id'])
    manager.store.execute("UPDATE runs SET harness='codex',model='openai/gpt-6.1-sol',active_model='openai/gpt-6.1-sol' WHERE id=?", (root,))
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    coordinator = attach(manager)
    result = await coordinator.fanout(manager.store.run(root), args)
    assert result['group_id'] == group['id']
    child = coordinator.children(group['id'])[0]
    assert (child['id'], child['harness'], child['model']) == (pinned['id'], 'hermes', 'test-model')


async def test_legacy_fanout_payload_remains_idempotent(durable):
    manager, cloud, root = durable
    coordinator, result, args = await launch(durable, count=1)
    legacy = args.model_dump()
    legacy.pop('harness')
    manager.store.execute("UPDATE agent_groups SET payload=?,runtime_assignments='' WHERE id=?",
                          (json.dumps(legacy), result['group_id']))
    repeated = await coordinator.fanout(manager.store.run(root), args)
    assert repeated['group_id'] == result['group_id']
    assert len(coordinator.children(result['group_id'])) == 1
    assert cloud.snapshots == 1


async def test_explicit_retry_keeps_original_worker_runtime_after_model_preference_change(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    manager.store.execute("UPDATE messages SET status='failed' WHERE run_id=?", (child['id'],))
    manager.store.execute("UPDATE runs SET status='failed',model='openai/gpt-6.1-sol' WHERE id=?", (child['id'],))
    args = Retry(group_id=result['group_id'], request_key='retry-original-runtime',
                 child_ids=[child['id']], instructions='Inspect prior work before retrying.')
    await coordinator.retry(manager.store.run(root), args)
    await coordinator.retry(manager.store.run(root), args)
    messages = manager.store.messages(child['id'])
    assert len(messages) == 2
    assert messages[-1]['model'] == 'test-model'
    assert manager.store.run(child['id'])['harness'] == child['harness']


async def test_nested_graph_and_frozen_handoff_keep_actual_runtime_metadata(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    await pause_parent(manager, root, result['group_id'])
    await drive(manager, child['id'], phase='monitor')
    nested = await coordinator.fanout(manager.store.run(child['id']), Fanout(request_key='nested-runtime', tasks=[
        {'label': 'Nested reviewer', 'prompt': 'Review the inherited work.', 'harness': 'opencode', 'model': 'opus'},
    ]))
    grandchild = coordinator.children(nested['group_id'])[0]
    view = coordinator.view(root, include_costs=False)
    node = view['groups'][0]['children'][0]['children'][0]
    assert (node['id'], node['harness'], node['model']) == (grandchild['id'], 'opencode', 'anthropic/claude-opus-5-5')
    manager.store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (grandchild['id'],))
    manager.store.update_run(grandchild['id'], status='idle', summary='Public worker answer')
    assert coordinator.handoff(child['id'], nested['group_id'])
    manager.store.execute("UPDATE runs SET model='openai/gpt-6.1-sol',summary='Later answer' WHERE id=?", (grandchild['id'],))
    frozen = coordinator.results(child['id'], nested['group_id'])['children'][0]
    assert frozen['model'] == 'anthropic/claude-opus-5-5'
    assert frozen['harness'] == 'opencode'
    assert frozen['summary'] == 'Public worker answer'


@pytest.mark.parametrize('status,expired', [('paused', False), ('stopped', False), ('active', True)])
async def test_inactive_ancestor_mission_rejects_nested_fanout_and_retry(durable, status, expired):
    from app.db import now
    from app.swarms import create_in

    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    await pause_parent(manager, root, result['group_id'])
    await drive(manager, child['id'], phase='monitor')
    with manager.store.connect() as conn:
        conn.begin_write()
        create_in(conn, root, 300, '2000-01-01T00:00:00+00:00' if expired else now())
        conn.execute('UPDATE swarm_missions SET status=? WHERE run_id=?', (status, root))
    with pytest.raises(ValueError, match='swarm is not active'):
        await coordinator.fanout(manager.store.run(child['id']), Fanout(request_key='too-late', tasks=[
            {'label': 'Late worker', 'prompt': 'Should not start.'},
        ]))
    assert not manager.store.rows('SELECT id FROM agent_groups WHERE parent_id=?', (child['id'],))
    manager.store.update_run(root, status='running')
    manager.store.update_run(child['id'], status='failed')
    manager.store.execute("UPDATE messages SET status='failed' WHERE run_id=?", (child['id'],))
    with pytest.raises(ValueError, match='swarm is not active'):
        await coordinator.retry(manager.store.run(root), Retry(group_id=result['group_id'],
            request_key='too-late-retry', child_ids=[child['id']], instructions='Should not restart.'))
    assert len(manager.store.messages(child['id'])) == 1


async def test_mission_expiring_during_snapshot_cannot_create_children(durable, monkeypatch):
    from app.db import now
    from app.swarms import create_in

    manager, cloud, root = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, root, phase='monitor')
    with manager.store.connect() as conn:
        conn.begin_write()
        create_in(conn, root, 300, now())
    snapshot = manager.snapshot_for_children
    async def expire_during_snapshot(run):
        result = await snapshot(run)
        manager.store.execute("UPDATE swarm_missions SET ends_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (root,))
        return result
    monkeypatch.setattr(manager, 'snapshot_for_children', expire_during_snapshot)
    with pytest.raises(ValueError, match='time budget has ended'):
        await coordinator.fanout(manager.store.run(root), Fanout(request_key='deadline-race', tasks=[
            {'label': 'Late worker', 'prompt': 'Should not start.'},
        ]))
    assert cloud.snapshots == 1
    assert not manager.store.rows('SELECT id FROM runs WHERE parent_run_id=?', (root,))


async def test_parent_releases_capacity_and_resumes_after_children_across_restart(durable):
    manager, cloud, run_id = durable
    manager.settings.max_concurrent_runs = 1
    coordinator, result, args = await launch(durable)
    children = coordinator.children(result['group_id'])
    assert await manager.advance(children[0]['id']) == 'capacity'
    await pause_parent(manager, run_id, result['group_id'])
    assert not cloud.machines[0].alive and manager.has_capacity()
    assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    coordinator = attach(manager)
    assert await manager.advance(run_id) == 'children'
    for child in children:
        await drive(manager, child['id'])
        assert cloud.machines[-1].spec['fresh_child'] is True
        assert cloud.machines[-1].spec['history_fallback'] == []
    await drive(manager, run_id)
    assert len(cloud.machines) == 7  # parent, five workers, resumed parent
    assert all(not m.alive for m in cloud.machines)
    assert manager.store.run(run_id)['status'] == 'idle'
    assert cloud.machines[-1].spec['agent_results']['completed'] == 5
    assert cloud.machines[-1].spec['continuation'] is True
    assert len([m for m in manager.store.messages(run_id) if m['role'] == 'assistant']) == 1


async def test_recovered_transport_does_not_reuse_old_checkpoint_after_child_handoff(durable):
    manager, cloud, run_id = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    command = cloud.command
    async def outage(machine, action, directory, value, **kwargs):
        if action == 'read' and len(cloud.launches) == 1:
            return json.dumps(transport_failure_report())
        return await command(machine, action, directory, value, **kwargs)
    cloud.command = manager.command = outage
    await drive(manager, run_id, phase='transport_wait')
    state = manager.state(run_id)
    state['retry_at'] = 0
    manager.save(run_id, state)
    await drive(manager, run_id, phase='monitor')
    assert cloud.machines[-1].spec['transport_recovery']
    result = await coordinator.fanout(manager.store.run(run_id), Fanout(
        request_key='after-recovery', instructions='Read the inherited file.', items=['A', 'B'], workers=2))
    children = coordinator.children(result['group_id'])
    await pause_parent(manager, run_id, result['group_id'])
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    attach(manager)
    for child in children:
        await drive(manager, child['id'])
    await drive(manager, run_id, phase='monitor')
    resumed = cloud.machines[-1].spec
    assert resumed['agent_results']['completed'] == 2
    assert resumed['continuation'] is True
    assert 'transport_recovery' not in resumed
    assert resumed['transport_attempt'] == 1  # A handoff cannot refund retries.
    assert len(manager.coordinator.children(result['group_id'])) == 2


@pytest.mark.parametrize('settle_before_reply', [False, True])
async def test_early_final_after_steering_keeps_worker_handoff_across_restart(durable, settle_before_reply):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    group = result['group_id']
    child = coordinator.children(group)[0]['id']
    await pause_parent(manager, root, group)
    original = manager.state(root)['message_id']
    target, _ = manager.store.enqueue_message(root, 'Apply the design skill and browser-test the migration.',
                                              'design-correction', user_id='google:tin')
    manager.message_queue.change(root, target['id'], 'google:tin', False, 0, 'steer')
    await drive(manager, root, phase='monitor')
    assert cloud.machines[-1].spec['agent_results']['settled'] is False
    packet = manager.message_queue.live_control(root, original, [])
    assert packet['input']['id'] == target['id']
    manager.message_queue.live_control(root, original, [target['id']])
    parent = cloud.machines[-1]
    parent.operations[manager.directory(manager.state(root))].update(
        message="I've read the skill. I'll apply it throughout the migration.")
    if settle_before_reply:
        await drive(manager, child)
    await drive(manager, root, phase='checkpointed')
    # The native turn ended, but neither the UI nor the scheduler may call the
    # original task complete before a durable handoff of the worker results.
    pending = json.loads(manager.store.run(root)['pending_result'])
    assert pending['completed'] is False
    assert pending['continuation'] is True and pending['wait_group'] == group
    assert not [m for m in manager.store.messages(root) if m['role'] == 'assistant']
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    coordinator = attach(manager)
    await drive(manager, root, phase='waiting_children')
    assert manager.state(root)['message_id'] == original
    assert manager.store.messages(root)[0]['status'] == 'running'
    assert not parent.alive
    if not settle_before_reply:
        assert await manager.advance(root) == 'children'
        await drive(manager, child)
    await drive(manager, root, phase='monitor')
    assert cloud.machines[-1].spec['agent_results']['result_scope'] == 'handoff'
    assert cloud.machines[-1].spec['agent_results']['children'][0]['summary'] == 'Saved answer'
    cloud.machines[-1].operations[manager.directory(manager.state(root))].update(
        message='Integrated worker changes and verified the migration.')
    await drive(manager, root)
    messages = manager.store.messages(root)
    assert [m['content'] for m in messages if m['role'] == 'assistant'] == [
        'Integrated worker changes and verified the migration.']
    assert all(m['status'] == 'completed' for m in messages)
    assert messages[1]['steering_parent_id'] == original
    assert len(coordinator.children(group)) == 1
    assert len(cloud.launches) == 4  # Initial parent, correction, child, integration.


async def test_new_requester_turn_keeps_the_existing_worker_obligation(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    group = result['group_id']
    await pause_parent(manager, root, group)
    target, _ = manager.store.enqueue_message(root, 'Also check accessibility.', 'new-requester', user_id='teammate')
    manager.message_queue.change(root, target['id'], 'teammate', False, 0, 'steer')
    await drive(manager, root)
    assert manager.store.messages(root)[0]['status'] == 'steered'
    await drive(manager, root, phase='monitor')
    assert manager.state(root)['message_id'] == target['id']
    assert not manager.state(root).get('resume_group')
    await drive(manager, root, phase='waiting_children')
    assert manager.state(root)['wait_group'] == group
    await drive(manager, coordinator.children(group)[0]['id'])
    await drive(manager, root)
    assert coordinator.results(root, group)['result_scope'] == 'handoff'
    assert len([m for m in manager.store.messages(root) if m['role'] == 'assistant']) == 1


async def test_old_completed_checkpoint_rechecks_outstanding_workers(durable):
    manager, cloud, root = durable
    _, result, _ = await launch(durable, count=1)
    await drive(manager, root, phase='checkpointed')
    state = manager.state(root)
    # Simulate an answer saved by an older server, before completion guarding.
    state['result'].update(completed=True, continuation=False)
    state['result'].pop('wait_group', None)
    manager.save(root, state)
    manager.store.update_run(root, pending_result=json.dumps(state['result']))
    successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    attach(successor)
    await drive(successor, root, phase='waiting_children')
    assert successor.state(root)['wait_group'] == result['group_id']
    assert successor.store.messages(root)[0]['status'] == 'running'
    assert not [m for m in successor.store.messages(root) if m['role'] == 'assistant']


@pytest.mark.parametrize('ending', ['cancel_group', 'stop', 'failure'])
async def test_completion_guard_respects_cancellation_and_failure(durable, ending):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    if ending == 'cancel_group':
        await coordinator.cancel_group(root, result['group_id'])
    elif ending == 'stop':
        await manager.cancel(root)
    else:
        cloud.machines[0].operations[cloud.launches[0]].update(completed=False, message='Model failed')
    await drive(manager, root)
    assert manager.store.run(root)['status'] == {
        'cancel_group': 'idle', 'stop': 'cancelled', 'failure': 'failed'}[ending]
    assert len(cloud.launches) == 1
    assert coordinator.group(root, result['group_id'])['status'] == 'cancelled'


async def test_hundred_active_slots_queue_the_101st_and_waiting_parent_is_free(durable):
    manager, cloud, root = durable
    manager.settings.max_concurrent_runs = 100
    ids = [root]
    for i in range(100):
        run = manager.store.create_run(f'Case {i}', '', 'modal', [], chat_enabled=True, model='test-model')
        manager.submit(run)
        ids.append(run['id'])
    results = await asyncio.gather(*(manager.advance(run_id) for run_id in ids))
    assert results.count('capacity') == 1
    assert len([r for r in manager.store.rows('SELECT state FROM durable_sessions') if json.loads(r['state']).get('phase') == 'provision']) == 100
    # Concurrent database reads can complete in any order. Resume the session
    # actually queued, rather than assuming it was the last task created.
    queued = ids[results.index('capacity')]
    occupied = next(run_id for run_id, result in zip(ids, results) if result != 'capacity')
    state = manager.state(occupied)
    state['phase'] = 'waiting_children'
    manager.save(occupied, state)
    assert await manager.advance(queued) is True
    assert not manager.has_capacity()
    assert not cloud.machines  # admission never eagerly creates a warm pool


async def test_parent_stop_cancels_queued_and_running_children_after_restart(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=2)
    await pause_parent(manager, root, result['group_id'])
    children = coordinator.children(result['group_id'])
    await drive(manager, children[0]['id'], phase='monitor')
    await manager.cancel(root)
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    attach(manager)
    for child in children:
        for _ in range(10):
            await manager.advance(child['id'])
            if manager.store.run(child['id'])['status'] == 'cancelled':
                break
        assert manager.store.run(child['id'])['status'] == 'cancelled'
        assert not manager.store.run(child['id'])['token_hash']
    await drive(manager, root)
    assert manager.store.run(root)['status'] == 'cancelled'
    assert all(not m.alive for m in cloud.machines)
    assert manager.store.rows('SELECT status FROM agent_groups')[0]['status'] == 'cancelled'


async def test_failed_workers_are_not_replayed_and_explicit_retry_is_idempotent(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    manager.store.execute("UPDATE messages SET status='failed' WHERE run_id=?", (child['id'],))
    manager.store.update_run(child['id'], status='failed', summary='One case failed')
    assert coordinator.results(root, result['group_id'])['completed'] == 0
    assert coordinator.settled(root, result['group_id'])
    args = Retry(group_id=result['group_id'], request_key='retry-once', child_ids=[child['id']], instructions='Inspect saved results and retry only the failed case.')
    await coordinator.retry(manager.store.run(root), args)
    await coordinator.retry(manager.store.run(root), args)
    messages = manager.store.messages(child['id'])
    assert len(messages) == 2 and messages[-1]['status'] == 'queued'
    assert messages[-1]['user_id'] == 'google:tin'
    assert not coordinator.settled(root, result['group_id'])


async def test_worker_artifacts_are_bounded_read_only_and_run_scoped(durable, tmp_path):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    directory = manager.settings.data_dir / 'artifacts'
    directory.mkdir()
    with zipfile.ZipFile(directory / (child['id'] + '.zip'), 'w') as archive:
        archive.writestr('new-files/results.json', '[{"case":1,"passed":true}]')
        archive.writestr('large.txt', 'x' * (128 * 1024 + 1))
    assert coordinator.read_artifact(root, Artifact(child_id=child['id']))['files'][0]['path'] == 'new-files/results.json'
    assert 'passed' in coordinator.read_artifact(root, Artifact(child_id=child['id'], path='new-files/results.json'))['content']
    for path in ['../secret', '/secret', 'large.txt']:
        with pytest.raises(ValueError):
            coordinator.read_artifact(root, Artifact(child_id=child['id'], path=path))
    with pytest.raises(ValueError, match='belong'):
        coordinator.read_artifact('unrelated-parent', Artifact(child_id=child['id']))


async def test_retry_waiting_on_lock_cannot_outlive_parent_cancellation(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]
    manager.store.update_run(child['id'], status='failed')
    lock = coordinator.locks[root]
    await lock.acquire()
    pending = asyncio.create_task(coordinator.retry(manager.store.run(root), Retry(
        group_id=result['group_id'], request_key='late-retry', child_ids=[child['id']], instructions='Inspect and retry.')))
    await asyncio.sleep(0)
    await manager.cancel(root)
    lock.release()
    with pytest.raises(ValueError, match='active Temporal'):
        await pending
    assert len(manager.store.messages(child['id'])) == 1
    assert manager.store.rows('SELECT * FROM agent_retries') == []


def test_orphan_child_cannot_receive_a_followup(workspace):
    app, client = workspace
    child = active(app)
    app.state.store.execute("UPDATE runs SET parent_run_id=?,mode='demo' WHERE id=?", ('parent', child['id']))
    response = client.post(f"/api/runs/{child['id']}/messages", json={'content':'Replace the answer', 'client_id':'override-child'})
    assert response.status_code == 409 and 'parent assignment' in response.json()['detail']
    assert len(app.state.store.messages(child['id'])) == 1


async def test_child_cost_rollup_does_not_duplicate_global_spend(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=2)
    spend = Spend(manager.store, manager.settings, None, None)
    ids = [root] + [c['id'] for c in coordinator.children(result['group_id'])]
    for i, run_id in enumerate(ids):
        request = spend.begin(manager.store.run(run_id), 'test-model')
        manager.store.execute("UPDATE model_requests SET cost=?,status='completed' WHERE id=?", (str(Decimal('0.1') * (i+1)), request))
    assert spend.report()['total']['spend'] == '0.6'
    manager.settings.litellm_api_key = 'rotated-key'
    view = coordinator.view(root)
    assert view['spend'] == '0.6'
    assert {c['cost']['spend'] for c in view['groups'][0]['children']} == {'0.2', '0.3'}
    assert coordinator.view(root, include_costs=False)['spend'] is None
    assert all('cost' not in c for c in coordinator.view(root, include_costs=False)['groups'][0]['children'])


async def test_agent_billing_states_match_spend_and_stay_hidden_from_members(
        durable: tuple[TemporalRunManager, object, str]) -> None:
    manager, _, root = durable
    coordinator, result, _ = await launch(durable, count=2)
    manager.settings.litellm_api_key = 'agent-recovery-fixture-key'
    manager.settings.litellm_api_base = 'https://gateway.example/v1'
    spend = Spend(manager.store, manager.settings, None, None)
    runs = [root] + [child['id'] for child in coordinator.children(result['group_id'])]
    requests = [spend.begin(manager.store.run(run_id), 'test-model') for run_id in runs]
    for request_id in requests:
        spend.finish(request_id, None, 'interrupted')
    manager.store.execute("UPDATE model_requests SET cost_recovery_error='receipt_access_denied' WHERE id=?", (requests[1],))
    manager.store.execute("UPDATE model_requests SET cost='0.2' WHERE id=?", (requests[2],))
    for pending, missing in ((1, 1), (0, 2)):
        total, view = spend.report()['total'], coordinator.view(root)
        for value in (total, view):
            assert (value['pending_costs'], value['missing_costs'], value['spend']) == (pending, missing, '0.2')
        children = {child['id']: child['cost'] for child in view['groups'][0]['children']}
        assert children[runs[1]]['missing_costs'] == 1 and children[runs[2]]['spend'] == '0.2'
        manager.settings.litellm_api_key = 'rotated-agent-fixture-key'
    hidden = coordinator.view(root, include_costs=False)
    assert hidden['spend'] is None and hidden['pending_costs'] == hidden['missing_costs'] == 0
    assert all('cost' not in child for child in hidden['groups'][0]['children'])


def test_agent_wait_interrupts_only_after_trusted_delegation_and_complete_tools():
    relay = SimpleNamespace(wait_group='')
    agent = SimpleNamespace(interrupt=lambda: calls.append('interrupt'))
    calls = []
    waiting = AgentWait(relay)
    waiting.step(agent)
    assert not calls
    relay.wait_group = 'f' * 32
    waiting.step(agent)
    waiting.step(agent)
    assert calls == ['interrupt']
    result = {'interrupted': True, 'messages': [{'tool_calls':[{'id':'call'}]}]}
    assert not waiting.can_continue(result)
    result['messages'].append({'role':'tool','tool_call_id':'call'})
    assert waiting.can_continue(result)


def test_broker_hides_delegation_without_temporal_and_keeps_run_scope(workspace):
    app, client = workspace
    root = active(app)
    headers = {'Authorization':'Bearer capability'}
    assert not any(t['name'].startswith('agents_') for t in client.get(f"/broker/{root['id']}/tools", headers=headers).json())
    app.state.settings.temporal_enabled = True
    assert any(t['name'] == 'agents_fanout' for t in client.get(f"/broker/{root['id']}/tools", headers=headers).json())
    other = active(app)
    app.state.store.update_run(other['id'], token_hash='different-capability')
    assert client.get(f"/broker/{other['id']}/tools", headers=headers).status_code == 401


@pytest.mark.parametrize('name', ['max_concurrent_runs', 'max_concurrent_model_requests'])
def test_capacity_configuration_supports_explicit_scale_but_requires_positive_budgets(name):
    assert getattr(Settings(_env_file=None, **{name: 3000}), name) == 3000
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{name: invalid})


async def test_direct_child_chat_joins_pending_work_and_keeps_sender_and_model(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    await pause_parent(manager, root, result['group_id'])
    child = coordinator.children(result['group_id'])[0]['id']
    await drive(manager, child, phase='monitor')
    message, created = coordinator.enqueue_child(child, 'Also check the edge case', 'direct-followup', 'test-model', 'google:bob')
    assert created
    assert coordinator.enqueue_child(child, 'Also check the edge case', 'direct-followup', 'test-model', 'google:bob')[1] is False
    assert coordinator.handoff(root, result['group_id']) is False
    # Finishing the assignment cannot resume the parent ahead of a queued chat.
    await drive(manager, child)
    assert manager.store.run(child)['status'] == 'queued'
    assert await manager.advance(root) == 'children'
    await drive(manager, child)
    assert manager.store.run(child)['active_user_id'] == 'google:bob'
    assert manager.store.run(child)['active_message_id'] == message['id']
    await drive(manager, root)
    assert coordinator.results(root, result['group_id'])['result_scope'] == 'handoff'
    assert len([m for m in manager.store.messages(child) if m['role'] == 'assistant']) == 2


async def test_handoff_rechecks_followup_arriving_during_capacity_wait(durable, monkeypatch):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    await pause_parent(manager, root, result['group_id'])
    child = coordinator.children(result['group_id'])[0]['id']
    await drive(manager, child)
    admission = manager.admission
    @asynccontextmanager
    async def admit(run_id):
        async with admission(run_id) as available:
            coordinator.enqueue_child(child, 'Check one more case', 'during-admission', None, 'google:bob')
            yield available
    monkeypatch.setattr(manager, 'admission', admit)
    assert await manager.advance(root) == 'children'
    assert manager.state(root)['phase'] == 'waiting_children'
    assert coordinator.group(root, result['group_id'])['result_snapshot'] == ''


@pytest.mark.parametrize('storage', ['local', 'remote', 'mixed'])
async def test_post_handoff_chat_preserves_answers_and_files_across_restart(durable, storage):
    from test_durable import aio
    from app.runner import RunManager
    from io import BytesIO
    manager, cloud, root = durable
    objects = MemoryObjects()
    if storage == 'remote':
        manager.store.objects = objects
    coordinator, result, _ = await launch(durable, count=1)
    await pause_parent(manager, root, result['group_id'])
    child = coordinator.children(result['group_id'])[0]['id']
    await drive(manager, child)
    first = BytesIO()
    with zipfile.ZipFile(first, 'w') as z:
        z.writestr('results.json', '{"passed":20}')
    manager.store.artifacts.save(child + '.zip', first.getvalue())
    objects.fail = True  # Freezing handoff metadata must never fetch an object.
    await drive(manager, root)
    assert objects.reads == 0
    objects.fail = False
    if storage == 'mixed':
        manager.store.objects = objects
    coordinator.enqueue_child(child, 'Change the follow-up report', 'new-report', None, 'google:bob')
    await drive(manager, child)
    manager.store.update_run(child, summary='New follow-up answer')
    # Exercise the real archive writer: replacing current files cannot mutate
    # a saved handoff, including after switching legacy storage to objects.
    content = BytesIO()
    with zipfile.ZipFile(content, 'w') as z:
        z.writestr('results.json', '{"passed":21}')
    async def stat(_): return SimpleNamespace(size=len(content.getvalue()))
    async def read(_): return content.getvalue()
    sandbox = SimpleNamespace(filesystem=SimpleNamespace(stat=aio(stat), read_bytes=aio(read)))
    await RunManager.save_artifact(manager, sandbox, child)
    if storage == 'mixed':
        frozen = json.loads(coordinator.group(root, result['group_id'])['result_snapshot'])[0]['artifact_name']
        with manager.store.connect() as conn:
            conn.begin_write()
            manager.store.artifacts.snapshot_in(conn, child + '.zip', frozen)
    restarted = Store(manager.settings.data_dir, object_storage=objects if storage != 'local' else None)
    coordinator = attach(cloud.attach(TemporalRunManager(restarted, manager.settings)))
    assert coordinator.results(root, result['group_id'])['children'][0]['summary'] == 'Saved answer'
    assert coordinator.results(root, result['group_id'], latest=True)['children'][0]['summary'] == 'New follow-up answer'
    assert coordinator.read_artifact(root, Artifact(child_id=child, path='results.json'))['content'] == '{"passed":20}'
    assert coordinator.read_artifact(root, Artifact(child_id=child, path='results.json', latest=True))['content'] == '{"passed":21}'
    assert coordinator.view(root, include_costs=False)['groups'][0]['children'][0]['status'] == 'idle'
    assert len([m for m in manager.store.messages(root) if m['role'] == 'assistant']) == 1
    if storage != 'local':
        objects.fail = True
        with pytest.raises(HTTPException, match='unavailable'):
            coordinator.read_artifact(root, Artifact(child_id=child, latest=True))
        with pytest.raises(ValueError, match='does not belong'):
            coordinator.read_artifact('unrelated-parent', Artifact(child_id=child))
        if storage == 'remote':
            assert not (manager.settings.data_dir / 'artifacts').exists()


async def test_child_chat_during_parent_stop_is_rejected_without_losing_assignment(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    child = coordinator.children(result['group_id'])[0]['id']
    manager.store.update_run(root, status='stopping')
    with pytest.raises(ValueError, match='finish stopping'):
        coordinator.enqueue_child(child, 'More work', 'while-stopping', None, 'google:bob')
    assert len(manager.store.messages(child)) == 1


@pytest.mark.parametrize('parent_state', ['stopping', 'legacy_completed', 'handed_off'])
async def test_historical_child_access_resolution_keeps_parent_admission_and_results(durable, parent_state):
    from fastapi import HTTPException
    from app.credentials import Resolve
    from test_credentials import attach as attach_credentials, access_request, generic_value

    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=1)
    vault = attach_credentials(manager)
    await pause_parent(manager, root, result['group_id'])
    child = coordinator.children(result['group_id'])[0]['id']
    await drive(manager, child, phase='monitor')
    request = access_request(vault, manager.store.run(child))
    await drive(manager, child)
    body = Resolve(scope='personal', lifetime='session', value=generic_value('env'), generation=0)
    if parent_state == 'stopping':
        manager.store.update_run(root, status='stopping')
        with pytest.raises(HTTPException) as error:
            vault.resolve(request['request_id'], body, 'google:tin', False)
        assert error.value.status_code == 409 and 'finish stopping' in error.value.detail
        assert vault.row(request['request_id'])['status'] == 'pending'
        assert not manager.store.rows('SELECT * FROM provider_secrets') and not manager.store.has_queued_messages(child)
        return
    if parent_state == 'handed_off':
        assert coordinator.handoff(root, result['group_id'])
    else:
        manager.store.execute("UPDATE agent_groups SET status='completed' WHERE id=?", (result['group_id'],))
    vault.resolve(request['request_id'], body, 'google:tin', False)
    queued = manager.store.rows("SELECT * FROM messages WHERE run_id=? AND status='queued'", (child,))
    assert len(queued) == 1 and queued[0]['user_id'] == 'google:tin'
    assert manager.store.rows('SELECT root_id FROM provider_secrets') == [{'root_id': root}]
    await drive(manager, child)
    manager.store.update_run(child, summary='New follow-up answer')
    assert coordinator.results(root, result['group_id'])['children'][0]['summary'] == 'Saved answer'
    assert coordinator.results(root, result['group_id'], latest=True)['children'][0]['summary'] == 'New follow-up answer'


async def test_cancelled_group_cannot_freeze_other_workers_that_are_still_stopping(durable):
    manager, cloud, root = durable
    coordinator, result, _ = await launch(durable, count=2)
    await pause_parent(manager, root, result['group_id'])
    first, other = [c['id'] for c in coordinator.children(result['group_id'])]
    await drive(manager, first)
    manager.store.execute("UPDATE agent_groups SET status='cancelled' WHERE id=?", (result['group_id'],))
    manager.store.update_run(other, status='stopping')
    coordinator.enqueue_child(first, 'Inspect your result', 'cancelled-group-chat', None, 'google:bob')
    assert not coordinator.group(root, result['group_id'])['result_snapshot']
    assert not coordinator.settled(root, result['group_id'])
    assert not coordinator.handoff(root, result['group_id'])


async def test_five_batches_delegate_reviewers_with_one_slot_and_restart(durable):
    manager, cloud, root = durable
    manager.settings.max_concurrent_runs = 1
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    manager.store.execute("UPDATE messages SET user_id='google:requester' WHERE run_id=?", (root,))
    await drive(manager, root, phase='monitor')
    group = await coordinator.call(root, 'agents_fanout', {
        'request_key': 'gauntlet-batches', 'instructions': 'Run gauntlet for the assigned PRs, then delegate independent review.',
        'items': [f'PR {i}' for i in range(23)], 'workers': 5})
    batches = coordinator.children(group['group_id'])
    assert len(batches) == 5
    partitions = [json.loads(manager.store.run(b['id'])['prompt'].split('indices):\n')[1].split('\nReturn a result')[0]) for b in batches]
    assert sorted(len(items) for items in partitions) == [4, 4, 5, 5, 5]
    assert sorted(item['index'] for items in partitions for item in items) == list(range(1, 24))
    await pause_parent(manager, root, group['group_id'])
    for batch in batches:
        await drive(manager, batch['id'], phase='monitor')
        assignments = {'request_key': 'independent-review', 'tasks': [
            {'label': 'Security reviewer', 'prompt': 'Review only this batch for security.'},
            {'label': 'Regression reviewer', 'prompt': 'Review only this batch for regressions.'}]}
        worker_limit, pending_limit = manager.settings.max_parallel_agents, manager.settings.max_pending_runs
        manager.settings.max_parallel_agents = 1
        with pytest.raises(ValueError, match='at most 1 workers'):
            await coordinator.call(batch['id'], 'agents_fanout', assignments)
        manager.settings.max_parallel_agents = worker_limit
        manager.settings.max_pending_runs = 1
        with pytest.raises(ValueError, match='queue is full'):
            await coordinator.call(batch['id'], 'agents_fanout', assignments)
        manager.settings.max_pending_runs = pending_limit
        assert not manager.store.rows('SELECT id FROM agent_groups WHERE parent_id=?', (batch['id'],))
        review = await coordinator.call(batch['id'], 'agents_fanout', assignments)
        reviewers = [row['id'] for row in coordinator.children(review['group_id'])]
        for reviewer in reviewers:
            assert manager.store.run(reviewer)['parent_run_id'] == batch['id']
            assert manager.store.root_id(reviewer) == root
            assert manager.store.run(reviewer)['owner_id'] == 'google:requester'
            assert await manager.advance(reviewer) == 'capacity'
        await pause_parent(manager, batch['id'], review['group_id'])
        assert manager.has_capacity() and not any(m.alive for m in cloud.machines)
        # Recover the entire chain from the same durable database, not Python tasks.
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        coordinator = attach(manager)
        assert await manager.advance(root) == 'children'
        for reviewer in reviewers:
            await drive(manager, reviewer)
        await drive(manager, batch['id'])
        assert {row['id'] for row in cloud.machines[-1].spec['agent_results']['children']} == set(reviewers)
    await drive(manager, root)
    assert manager.store.run(root)['status'] == 'idle'
    assert cloud.machines[-1].spec['agent_results']['completed'] == 5
    assert len(manager.store.subtree(root)) == 16
    assert not any(m.alive for m in cloud.machines)


async def nested(durable):
    manager, cloud, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    batch = coordinator.children(group['group_id'])[0]['id']
    await pause_parent(manager, root, group['group_id'])
    await drive(manager, batch, phase='monitor')
    review = await coordinator.call(batch, 'agents_fanout', {
        'request_key': 'review', 'tasks': [{'label': 'Reviewer', 'prompt': 'Review independently.'}]})
    return coordinator, group, batch, review, coordinator.children(review['group_id'])[0]['id']


async def test_nested_requester_scope_costs_and_frozen_artifacts(durable):
    from app.credentials import Resolve
    from test_credentials import attach as attach_credentials, access_request, generic_value
    manager, cloud, root = durable
    manager.store.execute("UPDATE runs SET owner_id='google:original-owner' WHERE id=?", (root,))
    coordinator, group, batch, review, reviewer = await nested(durable)
    vault = attach_credentials(manager)
    await pause_parent(manager, batch, review['group_id'])
    await drive(manager, reviewer, phase='monitor')
    request = access_request(vault, manager.store.run(reviewer))
    with manager.store.connect() as conn:
        assert [r['id'] for r in vault.pending_rows(conn, root, include_children=True)] == [request['request_id']]
    vault.resolve(request['request_id'], Resolve(scope='personal', lifetime='session', value=generic_value('env'), generation=0), 'google:tin', False)
    secret = manager.store.rows('SELECT * FROM provider_secrets')[0]
    assert secret['root_id'] == root
    assert vault.permitted(secret, manager.store.run(reviewer), 'google:tin')
    assert not vault.permitted(secret, manager.store.run(reviewer), 'google:other')
    assert not vault.permitted(secret, manager.store.run(reviewer), 'google:original-owner')
    unrelated = manager.store.create_run('Unrelated workflow', '', 'demo', [], user_id='google:tin')
    assert not vault.permitted(secret, unrelated, 'google:tin')
    # Direct group/artifact capabilities do not expand to arbitrary descendants.
    with pytest.raises(ValueError, match='belong'):
        coordinator.results(root, review['group_id'])
    with pytest.raises(ValueError, match='belong'):
        coordinator.read_artifact(root, Artifact(child_id=reviewer))
    await drive(manager, reviewer)
    while manager.store.has_queued_messages(reviewer):
        await drive(manager, reviewer)
    directory = manager.settings.data_dir / 'artifacts'
    directory.mkdir(exist_ok=True)
    with zipfile.ZipFile(directory / (reviewer + '.zip'), 'w') as archive:
        archive.writestr('review.txt', 'Frozen independent review')
    assert coordinator.handoff(batch, review['group_id'])
    with zipfile.ZipFile(directory / 'next.zip', 'w') as archive:
        archive.writestr('review.txt', 'Later follow-up')
    (directory / 'next.zip').replace(directory / (reviewer + '.zip'))
    assert coordinator.read_artifact(batch, Artifact(child_id=reviewer, path='review.txt'))['content'] == 'Frozen independent review'
    spend = Spend(manager.store, manager.settings, None, None)
    for identity in (root, batch, reviewer):
        request_id = spend.begin(manager.store.run(identity), 'test-model')
        manager.store.execute("UPDATE model_requests SET cost='0.1',status='completed' WHERE id=?", (request_id,))
    tree = coordinator.view(root)
    assert tree['spend'] == '0.3'
    assert tree['groups'][0]['children'][0]['cost']['spend'] == '0.2'
    assert tree['groups'][0]['children'][0]['children'][0]['cost']['spend'] == '0.1'
    assert spend.report()['total']['spend'] == '0.3'


async def test_nested_stop_fences_direct_chats_and_cancels_completed_group_followups(durable):
    manager, cloud, root = durable
    coordinator, group, batch, review, reviewer = await nested(durable)
    await pause_parent(manager, batch, review['group_id'])
    await drive(manager, reviewer)
    await drive(manager, batch)
    await drive(manager, root)
    coordinator.enqueue_child(reviewer, 'Inspect one more case', 'later-review', None, 'google:other')
    await drive(manager, reviewer, phase='monitor')
    manager.store.update_run(root, status='stopping')
    with pytest.raises(ValueError, match='finish stopping'):
        coordinator.enqueue_child(reviewer, 'Race with stop', 'stop-race', None, 'google:other')
    with pytest.raises(ValueError, match='finish stopping'):
        await coordinator.call(reviewer, 'agents_fanout', {'request_key': 'stop-race', 'tasks': [{'label': 'Late', 'prompt': 'Must not launch'}]})
    await manager.cancel(root)
    await drive(manager, reviewer)
    assert manager.store.run(reviewer)['status'] == 'cancelled'
    assert not manager.store.run(reviewer)['token_hash']
    assert not any(m.alive for m in cloud.machines)


def test_ancestry_survives_reopen_and_rejects_orphans_and_cycles(tmp_path):
    store = Store(tmp_path)
    root, child, leaf = [store.create_run('Legacy run', '', 'demo', [])['id'] for _ in range(3)]
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root, child))
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (child, leaf))
    store = Store(tmp_path)
    assert store.root_id(leaf) == root
    assert {r['id'] for r in store.subtree(child)} == {child, leaf}
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (leaf, root))
    with pytest.raises(ValueError, match='workflow root'):
        store.root_id(leaf)
    assert len(store.subtree(root)) == 3
    store.execute("UPDATE runs SET parent_run_id='missing' WHERE id=?", (root,))
    with pytest.raises(ValueError, match='workflow root'):
        store.root_id(leaf)
