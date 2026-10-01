"""Real Temporal dev-server tests; the Modal boundary is a deterministic fake."""
import asyncio
import json

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.agents import AgentCoordinator, Fanout
from app.db import Store
from app.session_workflow import SessionWorkflow
from app.temporal_runtime import TemporalRunManager
from test_durable import durable  # noqa: F401 -- shared fixture


async def eventually(predicate, seconds=60):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.1)


async def test_real_temporal_restarts_worker_and_drains_offline_followup(durable):
    manager, cloud, run_id = durable
    cloud.finished = False
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        await manager.recover()
        successor = None
        try:
            await eventually(lambda: manager.state(run_id).get('phase') == 'monitor', seconds=20)
            token_hash = manager.store.run(run_id)['token_hash']
            await manager.shutdown()
            assert cloud.machines[0].alive
            assert manager.store.run(run_id)['token_hash'] == token_hash
            manager.store.enqueue_message(run_id, 'A follow-up sent while offline', 'offline-followup')
            manager.submit(manager.store.run(run_id))
            cloud.finished = True
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.connect_temporal = connect
            await successor.recover()
            await eventually(lambda: len([m for m in successor.store.messages(run_id) if m['role'] == 'assistant']) == 2)
            assert len(cloud.machines) == len(cloud.launches) == len(cloud.terminations) == 2
            assert successor.store.run(run_id)['status'] == 'idle'
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            history = await handle.fetch_history()
            # Replaying a real execution history validates workflow determinism.
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            history_text = history.to_json()
            assert 'A follow-up sent while offline' not in history_text
            assert manager.settings.session_secret not in history_text
            await handle.terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()


async def test_real_temporal_fanout_waits_without_parent_machine_and_recovers(durable):
    manager, cloud, root = durable
    manager.settings.temporal_enabled = True
    manager.settings.max_concurrent_runs = 100
    cloud.saving_before_answer = False
    manager.coordinator = AgentCoordinator(manager.store, manager.settings, manager)
    original_command = cloud.command
    release_children = False
    peak = 0

    async def command(machine, action, directory, value, **kwargs):
        nonlocal peak
        peak = max(peak, sum(m.alive for m in cloud.machines))
        if action == 'read' and machine.spec.get('run_id') != root and not release_children:
            return json.dumps({'state': 'running', 'events': [], 'cursor': 0})
        return await original_command(machine, action, directory, value, **kwargs)

    cloud.finished = False
    manager.command = command
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        try:
            await manager.recover()
            await eventually(lambda: manager.state(root).get('phase') == 'monitor', seconds=25)
            result = await manager.coordinator.fanout(manager.store.run(root), Fanout(
                request_key='temporal-fanout', instructions='Check each case', workers=5, items=[str(i) for i in range(100)]))
            directory = manager.directory(manager.state(root))
            cloud.machines[0].operations[directory].update(completed=False, continuation=True, wait_group=result['group_id'])
            cloud.finished = True
            await eventually(lambda: manager.state(root).get('phase') == 'waiting_children', seconds=30)
            assert not cloud.machines[0].alive
            children = manager.coordinator.children(result['group_id'])
            await eventually(lambda: all(manager.state(c['id']).get('phase') == 'monitor' for c in children), seconds=30)
            assert sum(m.alive for m in cloud.machines) == 5
            await manager.shutdown()
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.coordinator = AgentCoordinator(successor.store, successor.settings, successor)
            successor.command = command
            successor.connect_temporal = connect
            release_children = True
            await successor.recover()
            await eventually(lambda: successor.store.run(root)['status'] == 'idle', seconds=60)
            assert len(cloud.machines) == len(cloud.launches) == len(cloud.terminations) == 7
            assert peak >= 5
            assert len([m for m in successor.store.messages(root) if m['role'] == 'assistant']) == 1
            handle = env.client.get_workflow_handle('moyai-session-' + root)
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
            for run_id in [root] + [c['id'] for c in children]:
                await env.client.get_workflow_handle('moyai-session-' + run_id).terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()


async def test_real_temporal_idle_timer_wakes_reuses_and_survives_restart(durable):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 6
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        try:
            await manager.recover()
            await eventually(lambda: manager.state(run_id).get('phase') == 'warm', seconds=25)
            first_deadline = manager.state(run_id)['idle_until']
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            await handle.signal(SessionWorkflow.wake)
            await asyncio.sleep(0.2)
            assert manager.state(run_id)['idle_until'] == first_deadline
            manager.store.enqueue_message(run_id, 'Within the idle window', 'warm-followup')
            manager.submit(manager.store.run(run_id))
            await eventually(lambda: len([m for m in manager.store.messages(run_id) if m['role'] == 'assistant']) == 2)
            assert len(cloud.machines) == 1 and len(cloud.launches) == 2
            assert cloud.launch_tokens[0] != cloud.launch_tokens[1]
            deadline = manager.state(run_id)['idle_until']
            await manager.shutdown()
            assert cloud.machines[0].alive
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.connect_temporal = connect
            await successor.recover()
            await eventually(lambda: successor.state(run_id).get('phase') == 'idle', seconds=20)
            assert len(cloud.terminations) == 1
            assert len([m for m in successor.store.messages(run_id) if m['role'] == 'assistant']) == 2
            history = await handle.fetch_history()
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            history_text = history.to_json()
            assert 'Within the idle window' not in history_text
            assert not any(token in history_text for token in cloud.launch_tokens)
            assert deadline >= first_deadline
            await handle.terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()


async def test_real_temporal_startup_retry_timer_survives_worker_replacement(durable):
    from test_startup_recovery import startup_report
    manager, cloud, run_id = durable
    command = cloud.command
    recovered = False
    async def startup_outage(machine, action, directory, value, **kwargs):
        if action == 'read' and not recovered:
            return json.dumps(startup_report())
        return await command(machine, action, directory, value, **kwargs)
    manager.command = startup_outage
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        try:
            await manager.recover()
            await eventually(lambda: manager.state(run_id).get('phase') == 'startup_wait', seconds=25)
            first_message = manager.state(run_id)['message_id']
            await manager.shutdown()
            assert cloud.machines[0].alive
            recovered = True
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.command = startup_outage
            successor.connect_temporal = connect
            await successor.recover()
            await eventually(lambda: successor.store.run(run_id)['status'] == 'idle', seconds=30)
            assert successor.state(run_id)['message_id'] == first_message
            assert len(cloud.launches) == 2 and len(cloud.machines) == 1
            assert [m['content'] for m in successor.store.messages(run_id) if m['role']=='assistant'] == ['Saved answer']
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            history = await handle.fetch_history()
            assert any(event.HasField('timer_started_event_attributes') for event in history.events)
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            assert not any(token in history.to_json() for token in cloud.launch_tokens)
            await handle.terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()
