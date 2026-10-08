import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import modal
from modal._utils.name_utils import check_object_name
import pytest

from app.config import MODEL_CATALOG, Settings
from app.db import Store
from app.main import public_messages
from app.durable_runner import DurableRunner
from app.temporal_runtime import TemporalRunManager
from app.runner import RunManager
from sandbox.durable_process import status, supervise


def aio(fn):
    return SimpleNamespace(aio=fn)


class Machine:
    def __init__(self, cloud, name, number):
        self.cloud, self.name = cloud, name
        self.object_id = f'sb-{number}'
        self.alive = True
        self.spec = {}
        self.operations = {}
        self.filesystem = SimpleNamespace(write_text=aio(self.write))
        self.poll = aio(self.poll_impl)
        self.terminate = aio(self.terminate_impl)
        self.wait = aio(self.wait_impl)
        self.snapshot_filesystem = aio(self.snapshot)

    async def write(self, data, path):
        if path.endswith('.json'):
            self.spec = json.loads(data)

    async def computer_request(self, body):
        assert self.alive and body['action'] == 'state'
        args = body['args']
        assert args['browser'] in {'restore', 'checkpoint'}
        return {'scope': args['scope'], **({'restored': True} if args['browser'] == 'restore' else {'state': None})}

    async def poll_impl(self):
        return None if self.alive else 0

    async def terminate_impl(self):
        self.alive = False
        self.cloud.terminations.append(self.object_id)

    async def wait_impl(self, **kwargs):
        assert not self.alive
        return 0

    async def snapshot(self, **kwargs):
        assert not self.cloud.saving_before_answer or self.cloud.store.run(self.cloud.run_id)['pending_result']
        if self.cloud.save_failures:
            self.cloud.save_failures -= 1
            raise TimeoutError('Snapshot timeout')
        self.cloud.snapshots += 1
        return SimpleNamespace(object_id=f'im-{self.cloud.snapshots}')


class Cloud:
    def __init__(self, monkeypatch, store, run_id):
        self.store, self.run_id = store, run_id
        self.machines, self.launches, self.terminations = [], [], []
        self.launch_tokens = []
        self.snapshots = self.save_failures = 0
        self.finished = True
        self.saving_before_answer = True
        self.lose_launch_ack = False
        self.continue_once = False
        monkeypatch.setattr('app.durable_runner.modal.Sandbox.from_id', aio(self.from_id))
        monkeypatch.setattr('app.durable_runner.modal.Sandbox.from_name', aio(self.from_name))
        monkeypatch.setattr('app.durable_runner.modal.Sandbox.create', aio(self.create))
        monkeypatch.setattr('app.durable_runner.modal.App.lookup', aio(self.app))
        monkeypatch.setattr('app.durable_runner.modal.Image.from_id', lambda *a, **kw: 'image')

    async def app(self, *args, **kwargs):
        return 'app'

    async def create(self, **kwargs):
        check_object_name(kwargs['name'], 'Sandbox')
        machine = Machine(self, kwargs['name'], len(self.machines))
        self.machines.append(machine)
        return machine

    async def from_name(self, app, name, **kwargs):
        for machine in self.machines:
            if machine.name == name and machine.alive:
                return machine
        raise modal.exception.NotFoundError('Missing')

    async def from_id(self, identity, **kwargs):
        return next(machine for machine in self.machines if machine.object_id == identity)

    async def command(self, machine, action, directory, value, *, token=None):
        if action == 'start':
            if directory not in machine.operations:
                self.launches.append(directory)
                self.launch_tokens.append(token)
                continuing = self.continue_once and len(self.launches) == 1
                machine.operations[directory] = {'kind': 'final', 'message': 'checkpoint' if continuing else 'Saved answer',
                                                 'completed': not continuing, 'continuation': continuing}
            if self.lose_launch_ack:
                self.lose_launch_ack = False
                raise ConnectionError('Lost acknowledgement after launch')
            return ''
        if not self.finished:
            return json.dumps({'state': 'running', 'events': [], 'cursor': 0})
        return json.dumps({'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 0,
                           'final': machine.operations[directory]})

    def attach(self, manager):
        async def client():
            return 'client'
        async def archive(*args):
            pass
        manager.client = client
        manager.image = lambda: 'image'
        manager.command = self.command
        manager.save_artifact = archive
        return manager


@pytest.fixture
def durable(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, session_secret='stable-test-key', agent_model='test-model', sandbox_idle_seconds=0)
    store = Store(tmp_path)
    run = store.create_run('Do the task', '', 'modal', [], chat_enabled=True)
    cloud = Cloud(monkeypatch, store, run['id'])
    manager = cloud.attach(TemporalRunManager(store, settings))
    manager.submit(run)
    return manager, cloud, run['id']


async def drive(manager, run_id, *, phase='idle', steps=40):
    for _ in range(steps):
        await manager.advance(run_id)
        if manager.state(run_id).get('phase') == phase:
            return
    pytest.fail(f'Never reached {phase}: {manager.state(run_id)}')


def transport_failure_report():
    return {'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 75, 'final': {
        'kind': 'final', 'message': 'Saving before reconnecting.', 'completed': False,
        'transport_retry': {'version': 1, 'checkpoint': {'epoch': 'test-epoch', 'seq': 3},
            'failure': {'version': 1, 'route': '/v1/messages', 'http_status': 502,
                        'transient': True, 'response_started': False, 'request_id': 'request-fixture'}}}}


@pytest.mark.parametrize('lost_machine', [False, True])
async def test_transport_recovery_checkpoints_and_resumes_same_turn_after_worker_loss(durable, lost_machine):
    manager, cloud, run_id = durable
    cloud.saving_before_answer = False
    original_command = cloud.command
    async def outage(machine, action, directory, value, **kwargs):
        if action == 'read' and len(cloud.launches) == 1:
            return json.dumps(transport_failure_report())
        return await original_command(machine, action, directory, value, **kwargs)
    cloud.command = outage
    manager.command = outage
    await drive(manager, run_id, phase='transport_wait')
    state = manager.state(run_id)
    assert cloud.snapshots == 1 and len(cloud.launches) == 1
    assert manager.store.run(run_id)['status'] == 'reconnecting'
    assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
    assert manager.store.messages(run_id)[0]['status'] == 'running'
    first_message, checkpoint = state['message_id'], state['snapshot_id']
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    wait = await manager.advance(run_id)
    assert 0 < wait['retry_seconds'] <= 2 and manager.state(run_id)['transport_attempt'] == 1
    if lost_machine:
        cloud.machines[0].alive = False
    state = manager.state(run_id)
    state['retry_at'] = 0
    manager.save(run_id, state)
    await drive(manager, run_id)
    assert len(cloud.launches) == 2 and cloud.launches[0] != cloud.launches[1]
    assert manager.state(run_id)['message_id'] == first_message
    spec = cloud.machines[-1].spec
    assert len(cloud.machines) == (2 if lost_machine else 1)
    assert spec['continuation'] is True and spec['prompt'] == 'Do the task'
    assert spec['transport_recovery'] == transport_failure_report()['final']['transport_retry']
    assert checkpoint == 'im-1' and cloud.snapshots == 2
    assert [m['content'] for m in manager.store.messages(run_id) if m['role'] == 'assistant'] == ['Saved answer']


@pytest.mark.parametrize('condition', ['stop', 'save_failure', 'partial_response', 'unknown_process', 'exhausted'])
async def test_transport_recovery_never_relaunches_without_safe_checkpoint(durable, condition):
    manager, cloud, run_id = durable
    cloud.saving_before_answer = False
    command = cloud.command
    async def outage(machine, action, directory, value, **kwargs):
        if action == 'read':
            report = transport_failure_report()
            if condition == 'partial_response':
                report['final']['transport_retry']['failure']['response_started'] = True
            if condition == 'unknown_process':
                return json.dumps({'state': 'uncertain', 'events': [], 'cursor': 0})
            return json.dumps(report)
        return await command(machine, action, directory, value, **kwargs)
    manager.command = outage
    await drive(manager, run_id, phase='monitor')
    if condition == 'save_failure':
        cloud.save_failures = 3
    if condition == 'exhausted':
        state = manager.state(run_id)
        state['transport_attempt'] = 3
        manager.save(run_id, state)
    for _ in range(15):
        try:
            await manager.advance(run_id)
        except TimeoutError:
            pass
        if manager.state(run_id).get('phase') == 'transport_wait':
            assert condition == 'stop'
            manager.store.update_run(run_id, status='stopping')
        if manager.state(run_id).get('phase') == 'idle':
            break
    assert manager.state(run_id)['phase'] == 'idle'
    assert len(cloud.launches) == 1
    assert manager.store.run(run_id)['status'] in {'failed', 'cancelled', 'interrupted'}


async def test_transport_recovery_bound_survives_segments_and_preserves_queued_input(durable):
    manager, cloud, run_id = durable
    cloud.saving_before_answer = False
    command = cloud.command
    async def outage(machine, action, directory, value, **kwargs):
        return json.dumps(transport_failure_report()) if action == 'read' else await command(machine, action, directory, value, **kwargs)
    cloud.command = outage
    manager.command = outage
    await drive(manager, run_id, phase='transport_wait')
    manager.store.enqueue_message(run_id, 'Keep the original constraints', 'queued-correction')
    for attempt in range(1, 4):
        state = manager.state(run_id)
        assert state['transport_attempt'] == attempt
        assert manager.store.has_queued_messages(run_id)
        state['retry_at'] = 0
        manager.save(run_id, state)
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        await drive(manager, run_id, phase='transport_wait' if attempt < 3 else 'idle')
    assert len(cloud.launches) == 4  # Original invocation plus three bounded continuations.
    assert manager.store.run(run_id)['status'] == 'failed'
    assert 'three continuation attempts' in manager.store.run(run_id)['error']


async def test_image_build_failure_finishes_once_without_exposing_provider_logs(durable, monkeypatch):
    manager, cloud, run_id = durable
    attempts = 0

    async def broken_image(**kwargs):
        nonlocal attempts
        attempts += 1
        raise modal.exception.ImageBuildError('private-build-log token=do-not-publish', 'im-broken')

    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create', aio(broken_image))
    manager.store.enqueue_message(run_id, 'Queued follow-up', 'queued-input')
    await drive(manager, run_id)
    row = manager.store.run(run_id)
    assert row['status'] == 'failed'
    assert 'workspace image could not be built' in row['error']
    assert row['token_hash'] == ''
    assert not cloud.launches and attempts == 1
    messages = manager.store.messages(run_id)
    assert not [m for m in messages if m['status'] in {'running', 'queued'}]
    assert 'private-build-log' not in json.dumps(messages + manager.store.events(run_id))
    assert await manager.advance(run_id) is False
    assert attempts == 1


async def test_transient_provision_error_remains_retryable(durable, monkeypatch):
    manager, cloud, run_id = durable

    async def disconnected(**kwargs):
        raise ConnectionError('Temporary network failure')

    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create', aio(disconnected))
    await drive(manager, run_id, phase='provision')
    with pytest.raises(ConnectionError):
        await manager.advance(run_id)
    assert manager.store.run(run_id)['status'] == 'provisioning'
    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create', aio(cloud.create))
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'idle'
    assert len(cloud.launches) == 1


async def test_every_step_can_lose_worker_and_launch_ack_without_repeating_work(durable):
    manager, cloud, run_id = durable
    cloud.lose_launch_ack = True
    failures = 0
    for _ in range(30):
        # New Python manager on every step: only the persisted state remains.
        manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
        try:
            await manager.advance(run_id)
        except ConnectionError:
            failures += 1
        if manager.state(run_id).get('phase') == 'idle':
            break
    assert failures == 1
    assert len(cloud.machines) == len(cloud.launches) == len(cloud.terminations) == 1
    assert manager.store.run(run_id)['status'] == 'idle'
    assert [m['content'] for m in manager.store.messages(run_id) if m['role'] == 'assistant'] == ['Saved answer']


async def test_final_receipt_is_visible_before_save_and_survives_worker_restart(durable: tuple[TemporalRunManager, Cloud, str]) -> None:
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='save')
    assert cloud.snapshots == 0
    # Reopen authoritative storage and replay the supervisor receipt.
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    row = manager.store.run(run_id)
    manager.receive_result(run_id, json.loads(row['pending_result']))
    assert len([e for e in manager.store.events(run_id) if e['message'] == 'Response received']) == 1
    answers = [m for m in public_messages(row, manager.store.messages(run_id)) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['content'] == 'Saved answer' and answers[0]['id'] < 0
    assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
    await drive(manager, run_id)
    answers = [m for m in public_messages(manager.store.run(run_id), manager.store.messages(run_id)) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['status'] == 'completed' and answers[0]['id'] > 0


async def test_queued_followup_restores_checkpoint_and_has_own_user_and_model(durable, monkeypatch):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='monitor')
    monkeypatch.setitem(MODEL_CATALOG, 'second-model', 'Second test model')
    manager.store.enqueue_message(run_id, 'Follow up', 'follow-up-id', model='second-model', user_id='second-user')
    await drive(manager, run_id)
    await drive(manager, run_id, phase='launch')
    assert manager.state(run_id)['snapshot_id'] == 'im-1'
    assert manager.store.run(run_id)['active_user_id'] == 'second-user'
    # Model is validated separately by the API; use a configured test model.
    await drive(manager, run_id)
    assert len(cloud.launches) == 2
    assert cloud.machines[1].spec['model'] == 'second-model'
    assert len([m for m in manager.store.messages(run_id) if m['role'] == 'assistant']) == 2


async def test_checkpoint_continues_same_turn_on_same_machine_then_rotates(durable):
    manager, cloud, run_id = durable
    cloud.continue_once = True
    await drive(manager, run_id, phase='checkpointed')
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'install'
    assert manager.state(run_id)['segment'] == 1
    assert len(cloud.machines) == 1
    assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
    await drive(manager, run_id)
    assert len(cloud.launches) == 2 and len(cloud.machines) == 1
    assert cloud.machines[0].spec['continuation'] is True


async def test_expired_machine_is_replaced_only_after_committed_checkpoint(durable):
    manager, cloud, run_id = durable
    cloud.continue_once = True
    await drive(manager, run_id, phase='checkpointed')
    state = manager.state(run_id)
    state['machine_started'] = 0
    manager.save(run_id, state)
    await manager.advance(run_id)
    assert not cloud.machines[0].alive
    assert manager.state(run_id)['phase'] == 'provision'
    await drive(manager, run_id)
    assert len(cloud.machines) == 2 and len(cloud.launches) == 2
    assert len([m for m in manager.store.messages(run_id) if m['role'] == 'assistant']) == 1


async def test_missing_machine_never_replays_unconfirmed_external_actions(durable):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='monitor')
    cloud.machines[0].alive = False
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'interrupted'
    assert len(cloud.launches) == len(cloud.machines) == 1


async def test_save_failure_keeps_answer_prior_snapshot_and_cancels_queue(durable):
    manager, cloud, run_id = durable
    manager.store.update_run(run_id, snapshot_id='im-before')
    await drive(manager, run_id, phase='save')
    manager.store.enqueue_message(run_id, 'Queued', 'queued-message')
    cloud.save_failures = 3
    for _ in range(2):
        with pytest.raises(TimeoutError):
            await manager.advance(run_id)
        assert manager.store.run(run_id)['summary'] == 'Saved answer'
    await drive(manager, run_id)
    assert manager.store.run(run_id)['snapshot_id'] == 'im-before'
    messages = manager.store.messages(run_id)
    assert [(message['role'], message['status']) for message in messages] == [
        ('user', 'save_failed'),
        ('assistant', 'save_failed'),
        ('user', 'cancelled'),
    ]
    assert 'Saved answer' in messages[1]['content']
    assert len(cloud.launches) == 1


async def test_cancellation_survives_worker_restart(durable):
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='monitor')
    await manager.cancel(run_id)
    assert not manager.store.run(run_id)['token_hash']
    manager = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'cancelled'
    assert not cloud.machines[0].alive
    assert len(cloud.launches) == 1


async def test_signal_outbox_retries_lost_ack_and_keeps_newer_wakes(durable):
    manager, cloud, run_id = durable
    sent = []
    async def start(*args, **kwargs):
        sent.append(kwargs['id'])
        if len(sent) == 1:
            raise ConnectionError()
        manager.submit(manager.store.run(run_id))  # A message races with ACK.
    manager.temporal = SimpleNamespace(start_workflow=start)
    with pytest.raises(ConnectionError):
        await manager.dispatch()
    assert manager.store.rows('SELECT delivered FROM durable_sessions')[0]['delivered'] == 0
    await manager.dispatch()
    row = manager.store.rows('SELECT * FROM durable_sessions')[0]
    assert row['revision'] > row['delivered']
    assert sent == ['moyai-session-' + run_id] * 2


async def test_stop_during_prepare_is_not_overwritten_and_rollback_refuses_active_work(durable):
    manager, cloud, run_id = durable
    started, release = asyncio.Event(), asyncio.Event()
    async def prepare(run_id):
        started.set()
        await release.wait()
    manager.prepare_context = prepare
    task = asyncio.create_task(manager.advance(run_id))
    await started.wait()
    with pytest.raises(RuntimeError, match='Drain Temporal'):
        await RunManager(manager.store, manager.settings).recover()
    await manager.cancel(run_id)
    release.set()
    await task
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'cancelled'
    assert not cloud.machines


def test_supervisor_launch_marker_prevents_second_execution_after_finished_or_abandoned(tmp_path):
    directory = tmp_path / 'operation'
    counter = tmp_path / 'effects.txt'
    script = "from pathlib import Path; p=Path(%r); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('WORKSPACE_EVENT '+%r)" % (str(counter), json.dumps({'kind': 'final', 'message': 'done', 'completed': True}))
    command = [sys.executable, '-c', script]
    supervise(directory, command)
    supervise(directory, command)
    assert counter.read_text() == 'x'
    report = status(directory)
    assert report['state'] == 'done' and report['final']['message'] == 'done'
    (directory / 'done.json').unlink()  # Lost supervisor after side effect.
    supervise(directory, command)
    assert status(directory)['state'] == 'uncertain'
    assert counter.read_text() == 'x'


async def test_sdk_diagnostics_survive_durable_result_and_activity_storage(durable):
    manager, cloud, run_id = durable
    diagnostic = {'version': 1, 'sdk': 'codex', 'source': 'native_error',
                  'code': 'httpConnectionFailed', 'http_status': 409, 'will_retry': False,
                  'pending_tools': 1, 'boundary_failed': True,
                  'boundary_reason': 'native output notification timed out', 'model_calls': 2}
    original = cloud.command
    async def command(machine, action, directory, value, **kwargs):
        result = await original(machine, action, directory, value, **kwargs)
        if action == 'read':
            report = json.loads(result)
            report['final'].update(completed=False, sdk_failure=diagnostic, message='Codex stopped (HTTP 409).')
            report['exit_code'] = 1
            report['events'] = [{'kind': 'error', 'message': 'Codex stopped (HTTP 409).',
                                 'data': {'activity_version': 1, 'phase': 'sdk_failure', **diagnostic}}]
            return json.dumps(report)
        return result
    manager.command = command
    await drive(manager, run_id, phase='checkpointed')
    persisted = json.loads(manager.store.run(run_id)['pending_result'])
    assert persisted['sdk_failure'] == diagnostic and persisted['completed'] is False
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'failed'
    assert len(cloud.launches) == 1, 'Diagnostic metadata alone must never authorize replay'
    event = next(e for e in manager.store.events(run_id) if e['data'].get('phase') == 'sdk_failure')
    assert all(event['data'][key] == value for key, value in diagnostic.items())
