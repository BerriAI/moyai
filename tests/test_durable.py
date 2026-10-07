import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import modal
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
