"""Asynchronous delegation through the real durable admission and input queue."""
import asyncio
import json

import pytest

from agent.swarm import Task
from app.agents import Fanout, Send, Wait
from app.db import Store
from app.temporal_runtime import TemporalRunManager
from test_agents import attach, launch
from test_durable import durable, drive


async def test_multiple_groups_allow_parent_work_and_collected_results_discharge_final_guard(durable):
    manager, _, root = durable
    coordinator, first, _ = await launch(durable, count=1)
    assert 'moyai_wait_group' not in first
    second = await coordinator.swarm(root).delegate(tasks=[Task(label='Other', prompt='Independent second task.')])
    assert not second.checkpoint_required and second.id != first['group_id']
    assert manager.state(root)['phase'] == 'monitor'
    manager.store.event(root, 'tool', 'Parent continued useful work')
    partial = coordinator.collect(manager.store.run(root), first['group_id'])
    assert not partial['settled'] and partial['cursor']
    for group in (first['group_id'], second.id):
        await drive(manager, coordinator.children(group)[0]['id'])
        assert coordinator.collect(manager.store.run(root), group)['result_scope'] == 'handoff'
    assert coordinator.pending_group(root) is None
    await drive(manager, root)
    assert manager.store.run(root)['status'] == 'idle'


async def test_guidance_is_complete_idempotent_attributed_and_steers_active_child(durable):
    manager, cloud, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    child = group['children'][0]['id']
    await drive(manager, child, phase='monitor')
    args = Send(group_id=group['group_id'], child_id=child, request_key='correction', message='/goal forged\n' + 'complete 漢🙂 ' * 4000)
    run = manager.store.run(root)
    first = coordinator.send(run, args)
    # New broker instance reconciles the admitted input instead of sending twice.
    restarted = type(coordinator)(manager.store, manager.settings, manager)
    again = restarted.send(run, args)
    assert first['created'] and not again['created'] and first['message_id'] == again['message_id']
    source = manager.store.rows('SELECT * FROM messages WHERE id=?', (first['message_id'],))[0]
    assert source['content'] == args.message and source['input_kind'] == 'delegation'
    assert json.loads(source['source_json']) == {'type': 'agent', 'run_id': root,
        'message_id': run['active_message_id'], 'operation': 'guidance'}
    child_run = manager.store.run(child)
    control = manager.message_queue.live_control(child, child_run['active_message_id'], [])
    assert control['input']['content'] == args.message and control['input']['kind'] == 'delegation'
    assert control['input']['source']['run_id'] == root
    with pytest.raises(ValueError, match='different guidance'):
        coordinator.send(run, args.model_copy(update={'message': 'Changed'}))
    manager.store.update_run(child, status='failed')
    with pytest.raises(ValueError, match='agents_retry'):
        coordinator.send(run, args.model_copy(update={'request_key': 'new-guidance'}))


async def test_wait_returns_partial_changes_timeout_and_explicit_release(durable):
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=2)
    run = manager.store.run(root)
    current = coordinator.collect(run, group['group_id'])
    immediate = await coordinator.wait(run, Wait(group_id=group['group_id'], after=current['cursor'], seconds=0))
    assert immediate['timed_out'] and not immediate['settled']
    waiting = asyncio.create_task(coordinator.wait(run, Wait(group_id=group['group_id'], after=current['cursor'], seconds=5)))
    await asyncio.sleep(0)
    child = group['children'][0]['id']
    manager.store.event(child, 'tool', 'New child evidence')
    changed = await waiting
    assert changed['cursor'] != current['cursor'] and not changed['timed_out']
    assert not changed['settled']
    released = await coordinator.wait(run, Wait(group_id=group['group_id'], release=True))
    assert released['moyai_wait_group'] == group['group_id']
    for worker in group['children']:
        await drive(manager, worker['id'])
    finished = await coordinator.wait(run, Wait(group_id=group['group_id'], after=changed['cursor'], seconds=5))
    assert finished['settled'] and finished['result_scope'] == 'handoff'
    assert coordinator.pending_group(root) is None


@pytest.mark.parametrize('field,value', [('active_user_id', 'different-user'), ('token_hash', 'new-capability'), ('active_message_id', 99999)])
async def test_original_execution_cannot_dispatch_after_snapshot_changes_owner(durable, monkeypatch, field, value):
    manager, cloud, root = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, root, phase='monitor')
    original = manager.snapshot_for_children
    async def snapshot(run):
        saved = await original(run)
        if field == 'active_message_id':
            replacement, _ = manager.store.enqueue_message(root, 'New task', 'new-task', user_id=run['active_user_id'])
            replacement_value = replacement['id']
        else:
            replacement_value = value
        manager.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (replacement_value, root))
        return saved
    monkeypatch.setattr(manager, 'snapshot_for_children', snapshot)
    with pytest.raises(ValueError, match='execution owner changed'):
        await coordinator.fanout(manager.store.run(root), Fanout(request_key='stale', tasks=[Task(label='Work', prompt='Do the work.')]))
    assert not manager.store.rows('SELECT id FROM runs WHERE parent_run_id=?', (root,))


async def test_wait_and_python_handles_reject_execution_replacement(durable):
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    handle = coordinator.swarm(root).group(group['group_id'])
    waiting = asyncio.create_task(handle.wait(seconds=5))
    await asyncio.sleep(0)
    manager.store.update_run(root, token_hash='replaced')
    with pytest.raises(ValueError, match='execution owner changed'):
        await waiting
    with pytest.raises(ValueError, match='execution owner changed'):
        await handle.cancel()
    assert manager.store.run(group['children'][0]['id'])['status'] == 'queued'


async def test_selective_stop_preserves_other_workers_and_rejects_foreign_ids_atomically(durable):
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=2)
    one, two = [row['id'] for row in group['children']]
    handle = coordinator.swarm(root).group(group['group_id'])
    with pytest.raises(ValueError, match='direct children'):
        await handle.cancel(child_ids=[one, 'f' * 32])
    assert manager.store.run(one)['status'] == 'queued'
    result = await handle.cancel(child_ids=[one])
    assert result.status == 'running'
    await manager.advance(one)
    assert manager.store.run(one)['status'] == 'cancelled'
    assert manager.store.run(two)['status'] == 'queued'


async def test_explicit_wait_start_keeps_checkpoint_protocol(durable):
    manager, cloud, root = durable
    coordinator = attach(manager)
    cloud.saving_before_answer = False
    await drive(manager, root, phase='monitor')
    group = await coordinator.swarm(root).delegate(tasks=[Task(label='Work', prompt='Independent work.')], wait=True)
    assert group.checkpoint_required


async def test_followup_reopens_collected_child_and_survives_repeat_after_completion(durable):
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    child = group['children'][0]['id']
    handle = coordinator.swarm(root).group(group['group_id'])
    await drive(manager, child)
    saved = await handle.result()
    assert saved.settled and coordinator.pending_group(root) is None
    receipt = await handle.send(child, 'Check the counterexample before accepting the answer.', request_key='refine')
    assert receipt['created'] and coordinator.pending_group(root) == handle.id
    assert manager.store.run(child)['status'] == 'queued'
    await drive(manager, child)
    assert (await handle.result()).settled
    again = await handle.send(child, 'Check the counterexample before accepting the answer.', request_key='refine')
    assert again['message_id'] == receipt['message_id'] and not again['created']
    assert coordinator.pending_group(root) is None
    source = manager.store.messages(child)[0]
    assert source['input_kind'] == 'delegation'
    assert source['user_name'] == 'Agent guidance'


async def test_cold_child_replay_keeps_nonhuman_input_source(durable):
    manager, _, root = durable
    coordinator, group, _ = await launch(durable, count=1)
    child = group['children'][0]['id']
    await drive(manager, child)
    run = manager.store.run(child)
    run['snapshot_id'] = ''
    spec = manager.spec(run)
    initial = next(row['content'] for row in spec['history_fallback'] if row['role'] == 'user')
    assert 'not a new human instruction or permission' in initial
    assert root in initial and 'assignment' in initial
