import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from pydantic import ValidationError

from app.agents import AgentCoordinator, Artifact, Fanout, Retry
from app.config import Settings
from app.db import Store
from app.spend import Spend
from app.temporal_runtime import TemporalRunManager
from sandbox.continuation import AgentWait
from test_durable import durable, drive
from test_workspace import workspace
from test_spend import active, sign_in


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
        assert child['plugins'] == '[]'
        assert child['snapshot_id'] == 'im-1'
        assert manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (child['id'],))[0]['revision'] == 1
        assert manager.store.messages(child['id'])[0]['user_id'] == 'google:tin'
        assert not coordinator.tools(manager.store.run(child['id']))
    with pytest.raises(ValueError, match='different work'):
        await coordinator.fanout(manager.store.run(run_id), args.model_copy(update={'workers': 3}))
    with pytest.raises(ValueError, match='enabled'):
        await coordinator.fanout(manager.store.run(run_id), args.model_copy(update={'model': 'unapproved'}))


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
    state = manager.state(root)
    state['phase'] = 'waiting_children'
    manager.save(root, state)
    assert await manager.advance(ids[-1]) is True
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
    with pytest.raises(ValueError, match='active top-level'):
        await pending
    assert len(manager.store.messages(child['id'])) == 1
    assert manager.store.rows('SELECT * FROM agent_retries') == []


def test_manual_child_followup_cannot_replace_the_assigned_result(workspace):
    app, client = workspace
    child = active(app)
    app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', ('parent', child['id']))
    response = client.post(f"/api/runs/{child['id']}/messages", json={'content':'Replace the answer', 'client_id':'override-child'})
    assert response.status_code == 409 and 'coordinator' in response.json()['detail']
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
    view = coordinator.view(root)
    assert view['spend'] == '0.6'
    assert {c['cost']['spend'] for c in view['groups'][0]['children']} == {'0.2', '0.3'}
    assert coordinator.view(root, include_costs=False)['spend'] is None
    assert all('cost' not in c for c in coordinator.view(root, include_costs=False)['groups'][0]['children'])


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


def test_broker_hides_delegation_without_temporal_and_denies_child_escalation(workspace):
    app, client = workspace
    root = active(app)
    headers = {'Authorization':'Bearer capability'}
    assert not any(t['name'].startswith('agents_') for t in client.get(f"/broker/{root['id']}/tools", headers=headers).json())
    app.state.settings.temporal_enabled = True
    assert any(t['name'] == 'agents_fanout' for t in client.get(f"/broker/{root['id']}/tools", headers=headers).json())
    app.state.store.execute("UPDATE runs SET parent_run_id='parent' WHERE id=?", (root['id'],))
    assert client.post(f"/broker/{root['id']}/tools/call", headers=headers, json={'name':'agents_fanout','arguments':{}}).status_code == 403


def test_capacity_configuration_accepts_100_not_unbounded():
    assert Settings(_env_file=None, max_concurrent_runs=100).max_concurrent_runs == 100
    assert Settings(_env_file=None, max_concurrent_model_requests=100).max_concurrent_model_requests == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_concurrent_runs=101)
