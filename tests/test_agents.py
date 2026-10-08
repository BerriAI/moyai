import asyncio
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
from sandbox.continuation import AgentWait
from test_durable import durable, drive
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
        assert child['plugins'] == '[]'
        assert child['snapshot_id'] == 'im-1'
        assert manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (child['id'],))[0]['revision'] == 1
        assert manager.store.messages(child['id'])[0]['user_id'] == 'google:tin'
        assert coordinator.tools(manager.store.run(child['id']))
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


def test_capacity_configuration_accepts_100_not_unbounded():
    assert Settings(_env_file=None, max_concurrent_runs=100).max_concurrent_runs == 100
    assert Settings(_env_file=None, max_concurrent_model_requests=100).max_concurrent_model_requests == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_concurrent_runs=101)


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
    async def admit(_):
        coordinator.enqueue_child(child, 'Check one more case', 'during-admission', None, 'google:bob')
        return True
    monkeypatch.setattr(manager, 'make_capacity', admit)
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
            conn.execute('BEGIN IMMEDIATE')
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
