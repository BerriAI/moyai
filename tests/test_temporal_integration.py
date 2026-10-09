"""Real Temporal dev-server tests; the Modal boundary is a deterministic fake."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.agents import AgentCoordinator, Fanout
from app.db import Store
from app.session_lifecycle import SessionLifecycle
from app.session_workflow import SessionWorkflow
from app.temporal_runtime import TemporalRunManager
from test_durable import durable  # noqa: F401 -- shared fixture
from test_workspace import recovery_catalog  # noqa: F401 -- actual broker declarations


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


@pytest.mark.parametrize('early_final', [False, True])
async def test_real_temporal_fanout_waits_without_parent_machine_and_recovers(durable, early_final):
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
        output = await original_command(machine, action, directory, value, **kwargs)
        if action == 'start' and machine.spec.get('agent_results', {}).get('settled') is False:
            machine.operations[directory]['message'] = "I've read the skill. I'll apply it."
        return output

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
            if early_final:
                cloud.finished = False
                original_message = manager.state(root)['message_id']
                target, _ = manager.store.enqueue_message(root, 'Apply the design skill.', 'design-correction')
                manager.message_queue.change(root, target['id'], '', False, 0, 'steer')
                manager.submit(manager.store.run(root))
                # Simulate the native receipt for the same-turn correction.
                await eventually(lambda: manager.state(root).get('phase') == 'monitor', seconds=30)
                packet = manager.message_queue.live_control(root, original_message, [])
                assert packet['input']['id'] == target['id']
                manager.message_queue.live_control(root, original_message, [target['id']])
                cloud.finished = True
                await eventually(lambda: manager.state(root).get('phase') == 'waiting_children', seconds=30)
                assert manager.state(root)['message_id'] == original_message
                assert not [m for m in manager.store.messages(root) if m['role'] == 'assistant']
            await manager.shutdown()
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.coordinator = AgentCoordinator(successor.store, successor.settings, successor)
            successor.command = command
            successor.connect_temporal = connect
            release_children = True
            await successor.recover()
            await eventually(lambda: successor.store.run(root)['status'] == 'idle', seconds=60)
            assert len(cloud.machines) == len(cloud.launches) == len(cloud.terminations) == (8 if early_final else 7)
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


@pytest.mark.parametrize('restart_state', ['warm', 'claimed', 'missing_journal', 'injected'])
async def test_real_temporal_restart_recovers_confirmed_delete_before_cleanup_starts(durable, restart_state):
    manager, cloud, run_id = durable
    manager.coordinator = AgentCoordinator(manager.store, manager.settings, manager)
    manager.settings.sandbox_idle_seconds = 300
    checkpoints = SimpleNamespace(flush=AsyncMock())
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = lifecycle = None
        try:
            if restart_state == 'warm':
                await manager.recover()
                await eventually(lambda: manager.state(run_id).get('phase') == 'warm', seconds=25)
            else:
                # A crash after claim commits, before begin_turn saves prepare.
                manager.save(run_id, {'phase': 'idle'})
                manager.store.claim_message(run_id)
                if restart_state == 'missing_journal':
                    manager.store.execute('DELETE FROM durable_sessions WHERE run_id=?', (run_id,))
                elif restart_state == 'injected':
                    # Legacy receipt left by an earlier stop; no active turn
                    # remains to own it or produce an assistant response.
                    manager.store.execute("UPDATE messages SET status='injected' WHERE run_id=?", (run_id,))
                    manager.store.execute('UPDATE durable_sessions SET delivered=revision WHERE run_id=?', (run_id,))
            saved = manager.store.messages(run_id)
            launches = len(cloud.launches)
            await manager.shutdown()
            # The process disappears after persisting confirmation, before
            # scheduling cleanup. Startup must reconstruct that operation.
            SessionLifecycle(manager.store, None, manager, checkpoints).request_delete(run_id, '', True)
            if restart_state == 'injected':
                manager.store.update_run(run_id, status='cancelled')
            assert manager.store.run(run_id)['deletion_requested_at']
            assert not manager.store.run(run_id)['deleted_at']
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.connect_temporal = connect
            lifecycle = SessionLifecycle(successor.store, None, successor, checkpoints)
            await successor.recover()
            lifecycle.start()
            await eventually(lambda: bool(successor.store.run(run_id)['deleted_at']), seconds=25)
            assert successor.state(run_id).get('phase', 'idle') == 'idle'
            assert len(cloud.launches) == launches
            assert not any(machine.alive for machine in cloud.machines)
            messages = successor.store.messages(run_id)
            if restart_state == 'warm':
                assert len(cloud.terminations) == 1 and messages == saved
            else:
                assert len(messages) == len(saved) and all(m['status'] == 'cancelled' for m in messages)
                assert [{k: v for k, v in m.items() if k != 'status'} for m in messages] == [
                    {k: v for k, v in m.items() if k != 'status'} for m in saved]
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
            await handle.terminate('Integration test complete')
        finally:
            if lifecycle:
                await lifecycle.close()
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
            handle = env.client.get_workflow_handle('moyai-session-' + run_id)
            # SQLite records startup_wait before the activity returns its
            # backoff. Replace the worker only after Temporal owns that timer,
            # or this can test activity replay without ever creating a timer.
            async with asyncio.timeout(10):
                while True:
                    history = await handle.fetch_history()
                    if any(event.HasField('timer_started_event_attributes') for event in history.events):
                        break
                    await asyncio.sleep(0.05)
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
            history = await handle.fetch_history()
            assert any(event.HasField('timer_started_event_attributes') for event in history.events)
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(history)
            assert not any(token in history.to_json() for token in cloud.launch_tokens)
            await handle.terminate('Integration test complete')
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()


@pytest.mark.parametrize('read_name,read_status', [('agents_results', 502), ('github_repositories', 524)])
async def test_real_temporal_model_recovery_timer_survives_worker_replacement(
        durable, tmp_path, monkeypatch, recovery_catalog, read_name, read_status):
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
            self.wfile.write(json.dumps(recovery_catalog).encode())
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(read_status if self.path == '/tools/call' else 502)
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
            agent.journal.tool_started('results', read_name, {})
            response = client.post('/tools/call', json={'name': read_name, 'arguments': {}})
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
