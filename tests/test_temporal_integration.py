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


@pytest.mark.parametrize('stage', ['workspace_tools', 'context_window'])
async def test_real_temporal_startup_retry_timer_survives_worker_replacement(durable, stage):
    from test_startup_recovery import startup_report
    manager, cloud, run_id = durable
    command = cloud.command
    recovered = False
    resumed_deadlines = []
    async def startup_outage(machine, action, directory, value, **kwargs):
        if action == 'read' and not recovered:
            return json.dumps(startup_report(stage=stage))
        if action == 'start' and recovered:
            state = successor.state(run_id)
            resumed_deadlines.append((state['turn_started'], state['startup_deadline']))
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
            first_state = manager.state(run_id)
            first_message = first_state['message_id']
            original_deadlines = (first_state['turn_started'], first_state['startup_deadline'])
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
            assert resumed_deadlines == [original_deadlines]
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


async def test_real_temporal_model_recovery_timer_survives_worker_replacement(durable, tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler
    from sandbox.startup import _read_with_reconnect
    from sandbox.transport_recovery import recovery_marker
    from test_broker_transport import diagnostic_relay
    from test_context_recovery import runtime
    from test_durable import transport_failure_report
    manager, cloud, run_id = durable
    cloud.saving_before_answer = False
    command = cloud.command
    failure = transport_failure_report()
    # Obtain the actual safe-read -> model outage classification and checkpoint;
    # only Modal execution remains synthetic in this Temporal restart test.
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps([{'name': 'agents_results', 'annotations': {
                'readOnlyHint': True, 'idempotentHint': True}}]).encode())
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'{}')
    def bounded_read(request, reader, **options):
        return _read_with_reconnect(request, reader, **{**options, 'budget': 0.01})
    monkeypatch.setattr('sandbox.broker_relay._read_with_reconnect', bounded_read)
    agent, _ = runtime(tmp_path)
    try:
        with diagnostic_relay(Edge) as (relay, client, diagnostics):
            agent.context.relay = relay
            assert client.get('/tools').status_code == 200
            agent.journal.tool_started('results', 'agents_results', {})
            response = client.post('/tools/call', json={'name': 'agents_results', 'arguments': {}})
            agent.journal.tool_finished('results', f'Tool failed (HTTP {response.status_code}).')
            assert not relay.uncertain_tool and not relay.last_error
            assert response.status_code == 503
            assert client.post('/v1/responses', json={}).status_code == 502
            marker = recovery_marker(agent, {'failed': True})
            assert marker and marker['failure'] == diagnostics[-1]
            failure['final']['transport_retry'] = marker
    finally:
        agent.context_store.close()
    failed_directory = None

    async def model_outage(machine, action, directory, value, **kwargs):
        nonlocal failed_directory
        if action == 'start' and failed_directory is None:
            failed_directory = directory
        if action == 'read' and directory == failed_directory:
            return json.dumps(failure)
        return await command(machine, action, directory, value, **kwargs)

    manager.command = model_outage
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        try:
            await manager.recover()
            await eventually(lambda: manager.state(run_id).get('phase') == 'transport_wait', seconds=25)
            first_message = manager.state(run_id)['message_id']
            checkpoint = manager.state(run_id)['snapshot_id']
            assert checkpoint and cloud.snapshots == 1
            assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            # Let the activity return its persisted backoff so this exercises
            # a real Temporal timer, not only a restart between activities.
            async with asyncio.timeout(10):
                while True:
                    history = await handle.fetch_history()
                    if any(event.HasField('timer_started_event_attributes') for event in history.events):
                        break
                    await asyncio.sleep(0.05)
            await manager.shutdown()
            assert cloud.machines[0].alive
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.command = model_outage
            successor.connect_temporal = connect
            await successor.recover()
            await eventually(lambda: successor.store.run(run_id)['status'] == 'idle', seconds=30)
            assert successor.state(run_id)['message_id'] == first_message
            assert len(cloud.launches) == 2 and len(cloud.machines) == 1
            assert cloud.launches[0] == failed_directory and cloud.launches[1] != failed_directory
            assert cloud.machines[0].spec['continuation'] is True
            assert cloud.machines[0].spec['transport_recovery'] == failure['final']['transport_retry']
            assert [m['content'] for m in successor.store.messages(run_id) if m['role'] == 'assistant'] == ['Saved answer']
            history = await handle.fetch_history()
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            history_text = history.to_json()
            assert not any(token in history_text for token in cloud.launch_tokens)
            assert 'transport_retry' not in history_text
            await handle.terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()


async def test_real_temporal_nested_waits_release_one_slot_and_replay_after_restart(durable):
    manager, cloud, root = durable
    manager.settings.temporal_enabled = True
    manager.settings.max_concurrent_runs = 1
    cloud.saving_before_answer = False
    manager.coordinator = AgentCoordinator(manager.store, manager.settings, manager)
    ready, peak = set(), 0

    async def command(machine, action, directory, value, **kwargs):
        nonlocal peak
        peak = max(peak, sum(m.alive for m in cloud.machines))
        if action == 'read' and machine.spec['run_id'] not in ready:
            return json.dumps({'state': 'running', 'events': [], 'cursor': 0})
        return await cloud.command(machine, action, directory, value, **kwargs)

    manager.command = command
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        ids = [root]
        try:
            await manager.recover()
            for parent in range(2):
                identity = ids[parent]
                await eventually(lambda: manager.state(identity).get('phase') == 'monitor', seconds=30)
                result = await manager.coordinator.call(identity, 'agents_fanout', {
                    'request_key': 'nested-review', 'tasks': [{'label': 'Batch' if parent == 0 else 'Reviewer', 'prompt': 'Verify assigned work independently.'}]})
                ids.append(manager.coordinator.children(result['group_id'])[0]['id'])
                machine = next(m for m in cloud.machines if m.alive and m.spec['run_id'] == identity)
                machine.operations[manager.directory(manager.state(identity))].update(completed=False, continuation=True, wait_group=result['group_id'])
                ready.add(identity)
                await eventually(lambda: manager.state(identity).get('phase') == 'waiting_children', seconds=30)
                assert not machine.alive
            await eventually(lambda: manager.state(ids[-1]).get('phase') == 'monitor', seconds=30)
            await manager.shutdown()
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.coordinator = AgentCoordinator(successor.store, successor.settings, successor)
            successor.command = command
            successor.connect_temporal = connect
            ready.add(ids[-1])
            await successor.recover()
            await eventually(lambda: successor.store.run(root)['status'] == 'idle', seconds=60)
            assert peak == 1 and len(cloud.machines) == 5
            assert not any(m.alive for m in cloud.machines)
            assert successor.store.root_id(ids[-1]) == root
            for identity in ids:
                handle = env.client.get_workflow_handle('moyai-session-' + identity)
                await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
                await handle.terminate('Nested integration complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()
