"""Real Temporal, native Codex, HTTP outage and durable execution journal.

Only Modal provisioning/snapshot RPCs and model inference are fixture boundaries.
"""
import asyncio
import json
import sys
import time

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.db import Store
from app.session_workflow import SessionWorkflow
from app.temporal_runtime import TemporalRunManager
from sandbox.durable_process import status, supervise
from test_durable import durable  # noqa: F401 -- existing Modal boundary fixture
from test_temporal_integration import eventually


NATIVE_PROCESS = '''
import json, sys, time
from pathlib import Path
from pytest import MonkeyPatch
from test_codex_sdk_transport import native_yield_case
root = Path(sys.argv[1])
def emit(kind, message, **data):
    print('WORKSPACE_EVENT ' + json.dumps({'kind': kind, 'message': message, **data}), flush=True)
def progress(message):
    if message.startswith('Status: '):
        return
    phase = {'phase': 'execution_started'} if message.startswith('Model request 1 admitted') else {}
    emit('status', message, data=phase)
def outage():
    (root / 'outage-until').write_text(str(time.monotonic() + 40))
with MonkeyPatch.context() as patch:
    proof = native_yield_case(root, patch, 1000, 'settle-transport-preview',
                             outage_seconds=40, progress=progress, on_outage=outage,
                             on_status=lambda kind, message, data: emit(kind, message, data=data))
(root / 'proof.json').write_text(json.dumps(proof))
emit('final', proof['final_response'], completed=proof['completed'],
     failed=proof['failed'], transport_attempt=proof['transport_attempt'])
'''


async def test_native_preview_recovers_after_real_temporal_worker_replacement(durable, tmp_path):
    manager, cloud, run_id = durable
    cloud.saving_before_answer = False
    native = tmp_path / 'native'
    native.mkdir()
    executions = {}
    command = cloud.command

    async def native_command(machine, action, directory, value, **kwargs):
        operation = tmp_path / 'executions' / directory.rsplit('/', 1)[-1]
        if action == 'start':
            await command(machine, action, directory, value, **kwargs)
            if directory not in executions:
                executions[directory] = asyncio.create_task(asyncio.to_thread(
                    supervise, operation, [sys.executable, '-c', NATIVE_PROCESS, str(native)]))
            return ''
        assert action == 'read'
        return json.dumps(await asyncio.to_thread(status, operation, int(value)))

    manager.command = native_command
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        async def connect():
            return env.client
        manager.connect_temporal = connect
        successor = None
        handle = env.client.get_workflow_handle('moyai-session-' + run_id)
        try:
            await manager.recover()
            await eventually(lambda: (native / 'outage-until').exists(), seconds=30)
            original = manager.state(run_id)
            token_hash = manager.store.run(run_id)['token_hash']
            assert original['phase'] == 'monitor'
            print('Broker unavailable; shutting down original Temporal worker', flush=True)
            await manager.shutdown()
            assert cloud.machines[0].alive and not next(iter(executions.values())).done()
            successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
            successor.command = native_command
            successor.connect_temporal = connect
            await successor.recover()
            await eventually(successor.ready.is_set, seconds=15)
            assert time.monotonic() < float((native / 'outage-until').read_text())
            assert successor.state(run_id)['message_id'] == original['message_id']
            assert successor.store.run(run_id)['token_hash'] == token_hash
            print('Replacement Temporal worker attached while original preview and broker outage continue', flush=True)
            await eventually(lambda: successor.store.run(run_id)['status'] in {'idle', 'failed', 'interrupted'}, seconds=85)
            proof = json.loads((native / 'proof.json').read_text())
            assert proof['completed'] and not proof['pending_tools'], proof
            assert proof['outage_elapsed'] >= 40
            assert proof['native_clients'] == proof['native_threads'] == 1
            assert proof['tool_executions'] == proof['completed_receipts'] == proof['command_starts'] == 1
            assert {sample['phase'] for sample in proof['preview_samples']} == {'before', 'during', 'after'}
            assert len({(sample['pid'], sample['port']) for sample in proof['preview_samples']}) == 1
            assert not proof['faults']
            assert len(executions) == len(cloud.machines) == len(cloud.launches) == 1
            assert successor.store.run(run_id)['status'] == 'idle'
            assert [message['content'] for message in successor.store.messages(run_id)
                    if message['role'] == 'assistant'] == ['yield-test-complete']
            await Replayer(workflows=[SessionWorkflow]).replay_workflow(await handle.fetch_history())
            print('PASS: original native process, preview, execution journal and Temporal workflow completed once', flush=True)
        finally:
            if successor:
                await successor.shutdown()
            await manager.shutdown()
            await asyncio.wait_for(asyncio.gather(*executions.values(), return_exceptions=True), timeout=95)
            await handle.terminate('Redeploy proof complete')
