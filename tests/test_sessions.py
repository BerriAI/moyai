import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.main import create_app, public_messages
from app.persistence import Checkpoints, restore_checkpoint
from app.runner import RunManager, completed_response
from test_runner import FakeSandbox, Lines, aio, runner
from test_workspace import wait_for, workspace


class SessionSandbox(FakeSandbox):
    def __init__(self, index):
        super().__init__()
        self.object_id = f'sb-session-{index}'
        self.index = index
        self.snapshot_filesystem = aio(self.snapshot)

    async def write(self, text, path):
        if path == '/tmp/task.json':
            await super().write(text, path)
        else:
            assert path.startswith('/opt/workspace-runner/')

    async def snapshot(self, *, timeout, ttl):
        assert ttl is None and timeout == 180
        return SimpleNamespace(object_id=f'im-session-{self.index}')


async def wait_jobs(manager):
    async with asyncio.timeout(5):
        while manager.jobs:
            await asyncio.sleep(0.01)


async def test_queued_followup_runs_once_with_saved_workspace_and_fresh_capability(runner, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    machines, images = [], []
    async def create(**kwargs):
        images.append(kwargs['image'])
        machine = SessionSandbox(len(machines) + 1)
        machines.append(machine)
        if len(machines) == 1:
            original = machine.exec.aio
            async def blocked(*args, **kwargs):
                started.set()
                await release.wait()
                return await original(*args, **kwargs)
            machine.exec = aio(blocked)
        return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    monkeypatch.setattr('app.runner.modal.Image.from_id', lambda image_id, **kwargs: image_id)
    run = runner.store.create_run('Create a file', '', 'modal', [], chat_enabled=True, model='openai/gpt-6-astra')
    runner.submit(run)
    runner.submit(run)
    await started.wait()
    token1 = runner.store.run(run['id'])['token_hash']
    message, created = runner.store.enqueue_message(run['id'], 'Read the same file', 'request-0002', 'anthropic/claude-opus-5-5')
    runner.submit(runner.store.run(run['id']))
    release.set()
    await wait_jobs(runner)
    assert created and len(machines) == 2
    assert images == ['fake image', 'im-session-1']
    assert machines[1].spec['prompt'] == 'Read the same file'
    assert [m.spec['model'] for m in machines] == ['openai/gpt-6-astra', 'anthropic/claude-opus-5-5']
    assert all(m.terminated for m in machines)
    row = runner.store.run(run['id'])
    assert row['status'] == 'idle' and row['snapshot_id'] == 'im-session-2'
    assert row['token_hash'] == '' and token1
    transcript = runner.store.messages(run['id'])
    assert len(transcript) == 4
    # Each executed user turn now stays with its answer, even when the second
    # request was enqueued before the first response had finished.
    assert [m['model'] for m in transcript] == ['openai/gpt-6-astra'] * 2 + ['anthropic/claude-opus-5-5'] * 2
    assert all(m['status'] == 'completed' for m in transcript)
    runner.store.enqueue_message(run['id'], 'Read the same file', 'request-0002')
    assert not runner.store.has_queued_messages(run['id'])


def make_rotation(machine):
    original = machine.exec.aio
    async def execute(*args, **kwargs):
        process = await original(*args, **kwargs)
        process.stdout = Lines(['WORKSPACE_EVENT {"kind":"final","message":"Work checkpointed; not finished yet.","completed":false,"continuation":true}\n'])
        return process
    machine.exec = aio(execute)


async def test_machine_renewal_continues_one_turn_without_publishing_a_partial_answer(runner, monkeypatch):
    machines, images = [], []
    async def create(**kwargs):
        images.append(kwargs['image'])
        assert kwargs['timeout'] == 86400
        if machines:
            assert machines[-1].terminated
        machine = SessionSandbox(len(machines) + 1)
        if not machines:
            make_rotation(machine)
        machines.append(machine)
        return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    monkeypatch.setattr('app.runner.modal.Image.from_id', lambda image_id, **kwargs: image_id)
    run = runner.store.create_run('Finish a long task', '', 'modal', [], chat_enabled=True)
    runner.store.enqueue_message(run['id'], 'Then summarize it', 'next-turn')
    runner.submit(run)
    await wait_jobs(runner)
    assert images == ['fake image', 'im-session-1', 'im-session-2']
    assert machines[1].spec['continuation'] is True
    assert machines[1].spec['prompt'] == 'Finish a long task'
    assert machines[1].spec['timeout'] is None
    assert machines[1].spec['rotation_seconds'] == 82800
    assert not machines[2].spec['continuation']
    answers = [m for m in runner.store.messages(run['id']) if m['role'] == 'assistant']
    assert len(answers) == 2 and all(m['content'] == 'Tests passed' for m in answers)
    assert runner.store.run(run['id'])['status'] == 'idle'
    assert all(machine.terminated for machine in machines)


@pytest.mark.parametrize('stop', [True, False])
async def test_machine_renewal_never_restarts_after_stop_or_failed_snapshot(runner, monkeypatch, stop):
    machine = SessionSandbox(1)
    make_rotation(machine)
    calls = []
    async def create(**kwargs):
        calls.append(1)
        return machine
    run = runner.store.create_run('Long task', '', 'modal', [], chat_enabled=True)
    async def snapshot(**kwargs):
        if stop:
            await runner.cancel(run['id'])
            return SimpleNamespace(object_id='im-saved')
        raise RuntimeError('Snapshot unavailable')
    machine.snapshot_filesystem = aio(snapshot)
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    runner.submit(run)
    await wait_jobs(runner)
    assert calls == [1] and machine.terminated
    assert runner.store.run(run['id'])['status'] in {'cancelled', 'failed'}


async def test_snapshot_failure_does_not_claim_saved_success_or_replay_followups(runner, monkeypatch):
    machine = SessionSandbox(1)
    async def broken_snapshot(**kwargs):
        raise RuntimeError('Snapshot storage unavailable')
    machine.snapshot_filesystem = aio(broken_snapshot)
    async def create(**kwargs):
        return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    run = runner.store.create_run('First response', '', 'modal', [], chat_enabled=True)
    runner.store.enqueue_message(run['id'], 'Queued followup', 'next-0001')
    runner.submit(run)
    await wait_jobs(runner)
    row = runner.store.run(run['id'])
    assert row['status'] == 'failed' and not row['snapshot_id'] and machine.terminated
    assert [m['status'] for m in runner.store.messages(run['id']) if m['role'] == 'user'] == ['save_failed', 'cancelled']
    answer = next(m for m in runner.store.messages(run['id']) if m['role'] == 'assistant')
    assert answer['content'].startswith('Tests passed')
    assert 'latest workspace files could not be saved' in answer['content']
    assert answer['status'] == 'save_failed'
    detail = next(e['data']['detail'] for e in runner.store.events(run['id']) if e['message'].startswith('Workspace save failed'))
    assert detail['stage'] == 'snapshot_filesystem' and detail['reason'] == 'Snapshot storage unavailable'


async def test_answer_is_durable_before_snapshot_and_survives_restart(runner, monkeypatch):
    import json
    machine = SessionSandbox(1)
    started, release = asyncio.Event(), asyncio.Event()
    async def blocked_snapshot(**kwargs):
        started.set()
        await release.wait()
        raise RuntimeError('Snapshot unavailable')
    machine.snapshot_filesystem = aio(blocked_snapshot)
    async def create(**kwargs): return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    run = runner.store.create_run('First response', '', 'modal', [], chat_enabled=True)
    runner.submit(run)
    await started.wait()
    # A new Store sees the answer on disk while the remote save is still blocked.
    reopened = Store(runner.settings.data_dir)
    row = reopened.run(run['id'])
    assert row['status'] == 'saving' and row['summary'] == 'Tests passed'
    assert json.loads(row['pending_result'])['message'] == 'Tests passed'
    visible = public_messages(row, reopened.messages(run['id']))
    assert [m['content'] for m in visible if m['role'] == 'assistant'] == ['Tests passed']
    assert len([m for m in reopened.messages(run['id']) if m['role'] == 'assistant']) == 1
    answer_id = visible[1]['id']
    assert answer_id > 0 and visible[1]['response_to_id'] == row['active_message_id']
    from app.runner import RunManager
    recovery = RunManager(reopened, runner.settings)
    recovery.settings = runner.settings.model_copy(update={'modal_token_id': ''})
    await recovery.recover()
    await recovery.recover()
    answers = [m for m in reopened.messages(run['id']) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['content'].startswith('Tests passed')
    assert answers[0]['status'] == 'save_failed' and answers[0]['id'] == answer_id
    release.set()
    await wait_jobs(runner)
    assert len([m for m in reopened.messages(run['id']) if m['role'] == 'assistant']) == 1


async def test_answer_notification_precedes_artifacts_without_releasing_followups(runner: RunManager, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    machine = SessionSandbox(1)
    async def create(**kwargs: object) -> SessionSandbox:
        return machine
    async def archive(*args: object) -> None:
        started.set()
        await release.wait()
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    monkeypatch.setattr(runner, 'save_artifact', archive)
    run = runner.store.create_run('Say hello', '', 'modal', [], chat_enabled=True)
    runner.submit(run)
    await started.wait()
    try:
        row = runner.store.run(run['id'])
        result = json.loads(row['pending_result'])
        runner.receive_result(run['id'], result)  # Replayed receipt is idempotent.
        receipts = [e for e in runner.store.events(run['id']) if e['message'] == 'Response received']
        assert len(receipts) == 1 and receipts[0]['data']['response_complete'] is True
        assert row['status'] == 'running'
        assert runner.store.messages(run['id'])[0]['status'] == 'running'
        runner.store.enqueue_message(run['id'], 'Next question', 'queued-next')
        assert runner.store.claim_message(run['id']) is None
        visible = public_messages(row, runner.store.messages(run['id']))
        assert [m['role'] for m in visible] == ['user', 'assistant', 'user']
        assert visible[1]['content'] == 'Tests passed' and visible[2]['status'] == 'queued'
        assert not row['snapshot_id']
        # Avoid launching the queued fixture again; cancellation follows existing policy.
        runner.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run['id'],))
    finally:
        release.set()
        await wait_jobs(runner)
    answers = [m for m in public_messages(runner.store.run(run['id']), runner.store.messages(run['id'])) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['id'] > 0 and answers[0]['status'] == 'completed'


@pytest.mark.parametrize('control', [None, 'continuation', 'steer_message_id', 'startup_retry', 'wait_group', 'wait_credential', 'incomplete', 'wrong_turn', 'missing_turn', 'empty', 'non_text'])
def test_response_receipt_marks_only_a_final_answer_for_the_active_turn(runner, control):
    run = runner.store.create_run('Say hello', '', 'modal', [], chat_enabled=True)
    message = runner.store.claim_message(run['id'])
    result = {'message_id': message['id'], 'completed': True, 'message': 'Hello!'}
    if control == 'incomplete':
        result['completed'] = False
    elif control == 'wrong_turn':
        result['message_id'] += 1
    elif control == 'missing_turn':
        result.pop('message_id')
    elif control == 'empty':
        result['message'] = ' '
    elif control == 'non_text':
        result['message'] = ['Hello!']
    elif control:
        result[control] = True
    runner.receive_result(run['id'], result)
    row = runner.store.run(run['id'])
    receipt = runner.store.events(run['id'])[-1]
    assert receipt['message'] == 'Response received'
    assert receipt['data']['response_complete'] is (control is None)
    assert row['status'] == 'queued'  # Receiving a result does not settle a turn.
    assert runner.store.messages(run['id'])[0]['status'] == 'running'
    answers = [m for m in runner.store.messages(run['id']) if m['role'] == 'assistant']
    assert len(answers) == (1 if control is None else 0)
    if answers:
        assert answers[0]['status'] == 'saving' and answers[0]['response_to_id'] == message['id']
    assert completed_response(row, []) is False
    assert completed_response(row, None) is False


@pytest.mark.parametrize('control', [None, 'continuation', 'steer_message_id', 'startup_retry', 'wait_group', 'wait_credential', 'incomplete', 'wrong_turn', 'empty', 'malformed', 'non_object'])
async def test_run_api_exposes_only_current_completed_answer_receipts(workspace: tuple[FastAPI, TestClient], control: str | None) -> None:
    app, client = workspace
    store = app.state.store
    run = store.create_run('Say hello', '', 'demo', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    result = {'message_id': message['id'], 'completed': True, 'message': 'Hello!', 'private_protocol_data': 'must-stay-private'}
    if control == 'incomplete':
        result['completed'] = False
    elif control == 'wrong_turn':
        result['message_id'] += 1
    elif control == 'empty':
        result['message'] = ' '
    elif control:
        result[control] = True
    raw = '{' if control == 'malformed' else '[]' if control == 'non_object' else json.dumps(result)
    store.update_run(run['id'], status='running', summary='Hello!', pending_result=raw)
    response = client.get('/api/runs/' + run['id'])
    assert response.status_code == 200 and 'must-stay-private' not in response.text
    assert 'pending_result' not in response.json()
    expected_status = 'running' if control else 'saving'
    assert response.json()['status'] == expected_status
    listed = next(row for row in client.get('/api/runs?scope=all').json() if row['id'] == run['id'])
    assert listed['status'] == expected_status and 'pending_result' not in listed
    # Compact sidebar/side-chat queries must project the same state and keep the
    # newly selected private result out of their public response.
    for relation in ('child', 'side'):
        related = store.create_run('Related answer', '', 'demo', [], chat_enabled=True,
                                   side_chat_of=run['id'] if relation == 'side' else '')
        if relation == 'child':
            store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], related['id']))
        related_message = store.claim_message(related['id'])
        related_result = {**result, 'message_id': related_message['id'] + (control == 'wrong_turn')}
        related_raw = raw if control in {'malformed', 'non_object'} else json.dumps(related_result)
        store.update_run(related['id'], status='running', pending_result=related_raw)
        if relation == 'child':
            parent = next(row for row in client.get('/api/runs?scope=all').json() if row['id'] == run['id'])
            projected = next(row for row in parent['children'] if row['id'] == related['id'])
        else:
            projected = client.get('/api/runs/' + run['id'] + '/side-chats').json()[0]
        assert projected['status'] == expected_status
        assert 'pending_result' not in projected and 'must-stay-private' not in json.dumps(projected)
        assert store.run(related['id'])['status'] == 'running'
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/runs/{run_id}/events')
    async def connected():
        return False
    async def streamed_status():
        request = SimpleNamespace(headers={}, cookies=dict(client.cookies), is_disconnected=connected)
        response = await endpoint(run['id'], request, after=store.events(run['id'])[-1]['id'])
        stream = response.body_iterator
        try:
            item = await anext(stream)
            assert item.startswith('event: run-status\n')
            assert 'must-stay-private' not in item and 'pending_result' not in item
            return json.loads(item.split('data: ', 1)[1])['status']
        finally:
            await stream.aclose()
    assert await streamed_status() == expected_status
    assert store.run(run['id'])['status'] == 'running'
    assert store.messages(run['id'])[0]['status'] == 'running'
    answers = [m for m in response.json()['messages'] if m['role'] == 'assistant']
    assert len(answers) == (0 if control else 1)
    if control:
        return
    assert answers[0]['id'] == -message['id'] and answers[0]['attachments'] == []
    assert answers[0]['status'] == 'saving' and answers[0]['content'] == 'Hello!'
    assert not [m for m in store.messages(run['id']) if m['role'] == 'assistant']
    store.enqueue_message(run['id'], 'Use a different model next', 'next-model', model='next-model')
    reply = next(m for m in client.get('/api/runs/' + run['id']).json()['messages'] if m['role'] == 'assistant')
    assert reply['model'] == message['model'] and reply['model'] != 'next-model'
    # A final answer receipt cannot mask a stopped/failed run or claim settlement.
    for status in ('failed', 'cancelled', 'interrupted', 'stopping'):
        store.update_run(run['id'], status=status)
        assert client.get('/api/runs/' + run['id']).json()['status'] == status
        assert await streamed_status() == status
    # Legacy cleanup may await termination after setting the run completed.
    store.update_run(run['id'], status='completed')
    assert len([m for m in client.get('/api/runs/' + run['id']).json()['messages'] if m['role'] == 'assistant']) == 1
    result['save_failed'] = True
    store.update_run(run['id'], summary='Hello!\nWorkspace save warning', pending_result=json.dumps(result))
    reply = next(m for m in client.get('/api/runs/' + run['id']).json()['messages'] if m['role'] == 'assistant')
    assert reply['status'] == 'save_failed' and reply['content'].endswith('Workspace save warning')
    stale_run = store.run(run['id'])
    store.finish_message(run['id'], message['id'], 'Hello!\nWorkspace save warning', 'save_failed')
    answers = [m for m in public_messages(stale_run, store.messages(run['id'])) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['id'] > 0 and answers[0]['status'] == 'save_failed'


async def test_failed_save_retains_prior_checkpoint_and_next_turn_uses_saved_chat(runner, monkeypatch):
    machines, specs = [], []
    async def create(**kwargs):
        machine = SessionSandbox(len(machines) + 1)
        machines.append(machine)
        if len(machines) == 1:
            async def broken(**kwargs): raise RuntimeError('Snapshot unavailable')
            machine.snapshot_filesystem = aio(broken)
        return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    monkeypatch.setattr('app.runner.modal.Image.from_id', lambda image_id, **kwargs: image_id)
    run = runner.store.create_run('First response', '', 'modal', [], chat_enabled=True)
    runner.store.update_run(run['id'], snapshot_id='im-previous-good')
    runner.submit(runner.store.run(run['id']))
    await wait_jobs(runner)
    assert runner.store.run(run['id'])['snapshot_id'] == 'im-previous-good'
    runner.store.enqueue_message(run['id'], 'Inspect what was saved; do not replay work.', 'inspect-next')
    runner.submit(runner.store.run(run['id']))
    await wait_jobs(runner)
    assert machines[1].spec['workspace_warning']
    assert any(m['role'] == 'assistant' and 'Tests passed' in m['content'] for m in machines[1].spec['history_fallback'])
    row = runner.store.run(run['id'])
    assert row['status'] == 'idle' and not row['checkpoint_error'] and row['snapshot_id'] == 'im-session-2'


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_failure_after_saved_checkpoint_keeps_answer_without_false_save_warning(runner, monkeypatch, failure):
    machine = SessionSandbox(1)
    async def create(**kwargs): return machine
    monkeypatch.setattr('app.runner.modal.Sandbox.create', aio(create))
    run = runner.store.create_run('Save before stopping', '', 'modal', [], chat_enabled=True)
    raised = False
    async def persist():
        nonlocal raised
        if runner.store.run(run['id'])['snapshot_id'] and not raised:
            raised = True
            raise failure()
    runner.persist = persist
    runner.submit(run)
    await wait_jobs(runner)
    row = runner.store.run(run['id'])
    answer = next(m for m in runner.store.messages(run['id']) if m['role'] == 'assistant')
    assert answer['content'] == 'Tests passed'
    assert not row['checkpoint_error'] and row['snapshot_id'] == 'im-session-1'
    assert machine.terminated


async def test_cancel_before_chat_driver_starts_cannot_restart_it(runner):
    run = runner.store.create_run('Stop before boot', '', 'demo', [], chat_enabled=True)
    await runner.cancel(run['id'])
    runner.submit(runner.store.run(run['id']))
    await wait_jobs(runner)
    assert runner.store.run(run['id'])['status'] == 'cancelled'
    assert runner.store.messages(run['id'])[0]['status'] == 'cancelled'
    assert len(runner.store.messages(run['id'])) == 1


def test_message_idempotency_is_transactional_and_conflicts_are_rejected(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Initial', '', 'demo', [], chat_enabled=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.enqueue_message(run['id'], 'Follow up', 'same-request'), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({message['id'] for message, _ in results}) == 1
    with pytest.raises(ValueError, match='different text'):
        store.enqueue_message(run['id'], 'Different', 'same-request')


@pytest.mark.parametrize('previous_status', ['completed', 'failed', 'save_failed'])
@pytest.mark.parametrize('saved_receipt', [False, True])
async def test_restart_of_unclaimed_followup_displays_current_failure_not_previous_answer(
    runner: RunManager, previous_status: str, saved_receipt: bool,
) -> None:
    from pathlib import Path
    import shutil
    import subprocess
    from app.main import public_run

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to exercise the real browser failure selector')
    store = runner.store
    run = store.create_run('First request', '', 'demo', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    previous_answer = {'completed': 'Previous successful answer', 'failed': 'Previous failed answer',
                       'save_failed': 'Previous answer with an unsaved workspace'}[previous_status]
    previous_warning = 'The previous turn workspace files could not be saved.'
    if saved_receipt:
        # A normal cloud completion retains this receipt until the next claim.
        runner.receive_result(run['id'], {'message_id': first['id'], 'message': previous_answer,
            'completed': previous_status != 'failed', 'exit_code': 1 if previous_status == 'failed' else 0,
            'checkpoint_saved': previous_status != 'save_failed'})
        if previous_status == 'save_failed':
            assert runner.preserve_answer(run['id'], previous_warning)
            previous_answer = store.run(run['id'])['summary']
            assert json.loads(store.run(run['id'])['pending_result'])['save_failed'] is True
    elif previous_status == 'save_failed':
        store.update_run(run['id'], checkpoint_error=previous_warning)
        previous_answer += '\n\n---\n**Workspace save warning:** ' + previous_warning
    store.finish_message(run['id'], first['id'], previous_answer, previous_status)
    store.update_run(run['id'], status='idle' if previous_status == 'completed' else 'failed', summary=previous_answer)
    followup, _ = store.enqueue_message(run['id'], 'Follow-up not claimed before restart', 'unclaimed-followup')
    queued = store.run(run['id'])
    assert queued['status'] == 'queued' and queued['summary'] == previous_answer
    assert queued['active_message_id'] == first['id']
    assert bool(queued['pending_result']) is saved_receipt

    recovery = RunManager(Store(runner.settings.data_dir), runner.settings)
    await recovery.recover()
    stopped = recovery.store.run(run['id'])
    messages = public_messages(stopped, recovery.store.messages(run['id']))
    assert stopped['status'] == 'interrupted'
    assert stopped['summary'] == previous_answer
    assert stopped['error'] == 'The workspace restarted. This task was not replayed.'
    if previous_status == 'save_failed':
        # Preserve the warning in its saved answer and workspace state without
        # letting it own this follow-up's interruption reason.
        assert stopped['checkpoint_error'] == previous_warning
        assert previous_warning in next(message['content'] for message in messages
                                        if message['role'] == 'assistant' and message['status'] == 'save_failed')
    assert messages[-1]['id'] == followup['id'] and messages[-1]['status'] == 'interrupted'
    assert messages[-1]['started_at'] == ''
    assert len([message for message in messages if message['role'] == 'assistant']) == 1
    snapshot = {**public_run(stopped), 'messages': messages,
                'events': recovery.store.events(run['id'])}
    result = subprocess.run([node, '-e',
        "const fs=require('node:fs'),ui=require(process.argv[1]),run=JSON.parse(fs.readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify({answer:ui.terminalAnswer(run),error:ui.terminalError(run)}));",
        str(Path(__file__).resolve().parents[1] / 'app/static/activity.js')],
        input=json.dumps(snapshot), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {'answer': None, 'error': stopped['error']}


async def test_restart_preserves_idle_session_and_interrupts_unfinished_messages(runner):
    # This checks recovery, not the demo's four seconds of presentation delays.
    runner.settings.demo_step_seconds = 0
    saved = runner.store.create_run('Saved chat', '', 'demo', [], chat_enabled=True)
    runner.submit(saved)
    await wait_jobs(runner)
    pending = runner.store.create_run('Unfinished', '', 'demo', [], chat_enabled=True)
    runner.store.claim_message(pending['id'])
    # Crash in the boundary after agent completion but before committing the reply.
    runner.store.update_run(pending['id'], status='completed')
    await runner.recover()
    assert runner.store.run(saved['id'])['status'] == 'idle'
    assert runner.store.run(pending['id'])['status'] == 'interrupted'
    assert runner.store.messages(pending['id'])[0]['status'] == 'interrupted'
    assert not runner.jobs


@pytest.mark.parametrize('operation', ['recover', 'stop_for_deletion', 'cancel'])
@pytest.mark.parametrize('source_status', ['running', 'interrupted', 'cancelled', 'completed', 'steered'])
@pytest.mark.parametrize('receipt', ['missing', 'unrelated'])
async def test_orphaned_published_answer_settles_without_replay_or_replacing_history(
    runner: RunManager, operation: str, source_status: str, receipt: str,
) -> None:
    store = runner.store
    run = store.create_run('Previous request', '', 'demo', [], chat_enabled=True)
    previous = store.claim_message(run['id'])
    store.finish_message(run['id'], previous['id'], 'Previous saved answer')
    store.update_run(run['id'], status='idle')
    settled = store.messages(run['id'])
    current, _ = store.enqueue_message(run['id'], 'Current request', 'orphaned-answer')
    assert store.claim_message(run['id'])['id'] == current['id']
    runner.receive_result(run['id'], {'message_id': current['id'], 'message': 'Current published answer',
                                     'completed': True, 'exit_code': 0})
    published = store.messages(run['id'])[-1]
    assert published['role'] == 'assistant' and published['status'] == 'saving'
    assert published['response_to_id'] == current['id']
    # Model a lost job/receipt, or an older restart sweep that settled only the
    # input. Terminal runs have no queued input to make recovery discover them.
    queued = None
    if source_status == 'running':
        queued, _ = store.enqueue_message(run['id'], 'Do not replay this', 'orphaned-followup')
    else:
        store.execute('UPDATE messages SET status=? WHERE id=?', (source_status, current['id']))
    stale = {'message_id': previous['id'], 'message': 'Unrelated retained answer', 'completed': True,
             'exit_code': 0, 'checkpoint_saved': False}
    run_status = ('interrupted' if operation == 'cancel' else 'saving') if source_status == 'running' else source_status
    store.update_run(run['id'], status=run_status,
                     pending_result=json.dumps(stale) if receipt == 'unrelated' else '',
                     summary='Unrelated retained summary')

    recovery = RunManager(Store(runner.settings.data_dir), runner.settings)
    assert not recovery.jobs
    await getattr(recovery, operation)(*([run['id']] if operation != 'recover' else []))
    messages = recovery.store.messages(run['id'])
    assert messages[:2] == settled
    terminal = ('interrupted' if operation == 'recover' else 'cancelled') if source_status == 'running' else source_status
    answer = next(message for message in messages if message['id'] == published['id'])
    assert answer == {**published, 'status': terminal}
    assert next(message for message in messages if message['id'] == current['id'])['status'] == terminal
    assert len([message for message in messages if message['role'] == 'assistant']) == 2
    if queued:
        followup = next(message for message in messages if message['id'] == queued['id'])
        assert followup['status'] == ('interrupted' if operation == 'recover' else 'cancelled')
        assert followup['started_at'] == ''
    assert not recovery.jobs and not recovery.store.has_queued_messages(run['id'])
    assert recovery.store.claim_message(run['id']) is None
    # Cleanup/recovery retry must retain the same terminal receipts.
    await getattr(recovery, operation)(*([run['id']] if operation != 'recover' else []))
    assert recovery.store.messages(run['id']) == messages


def test_followup_api_keeps_one_session_and_enforces_auth_csrf_and_legacy_boundary(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787', demo_step_seconds=0.01,
                        workspace_password='', modal_token_id='', modal_token_secret='', litellm_api_key='')
    app = create_app(settings)
    with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 51000)) as client:
        session = client.get('/api/session').json()
        client.headers.update({'Origin': settings.public_url, 'X-CSRF-Token': session['csrf']})
        run = client.post('/api/runs', json={'prompt':'Start a conversation','mode':'demo'}).json()
        wait_for(lambda: app.state.store.run(run['id'])['status'] == 'idle')
        endpoint = f"/api/runs/{run['id']}/messages"
        message = {'content':'Continue please','client_id':'followup-123'}
        assert client.post(endpoint, json=message, headers={'X-CSRF-Token':''}).status_code == 403
        assert client.post(endpoint, json=message).status_code == 202
        assert client.post(endpoint, json=message).json()['created'] is False
        wait_for(lambda: app.state.store.run(run['id'])['status'] == 'idle')
        result = client.get(f"/api/runs/{run['id']}").json()
        assert len(result['messages']) == 4 and result['chat_enabled']
        assert len(client.get('/api/runs').json()) == 1
        old = app.state.store.create_run('Legacy task','', 'demo', [])
        assert client.post(f"/api/runs/{old['id']}/messages",json=message).status_code == 409
        client.cookies.clear()
        assert client.post(endpoint,json=message).status_code == 401


@pytest.mark.sqlite_only
async def test_checkpoints_replace_latest_session_archive_and_restore_chat(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path/'data', checkpoint_dir=tmp_path/'checkpoints')
    store = Store(settings.data_dir)
    run = store.create_run('Persist chat','', 'demo', [], chat_enabled=True)
    directory = settings.data_dir/'artifacts';directory.mkdir()
    archive = directory/f"{run['id']}.zip";archive.write_bytes(b'first turn')
    async def commit(): pass
    checkpoints = Checkpoints(store, settings, commit=commit)
    await checkpoints.flush()
    archive.write_bytes(b'newest files from second turn')
    store.enqueue_message(run['id'],'Second turn','second-turn')
    await checkpoints.flush()
    settings.data_dir=tmp_path/'restored'
    restore_checkpoint(settings)
    restored=Store(settings.data_dir)
    assert len(restored.messages(run['id'])) == 2
    assert (settings.data_dir/'artifacts'/archive.name).read_bytes() == b'newest files from second turn'


def settled_session(app, owner='google:bob', **kwargs):
    store = app.state.store
    run = store.create_run('Settled session', '', 'demo', [], chat_enabled=True, user_id=owner, **kwargs)
    store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (run['id'],))
    store.update_run(run['id'], status='idle')
    return run['id']


def test_delete_requires_exact_creator_or_admin_and_preserves_records(workspace):
    from app.db import now
    from test_spend import sign_in

    app, client = workspace
    store = app.state.store
    run_id = settled_session(app)
    sign_in(app, client, 'other', 'other@berri.ai')
    url = '/api/runs/' + run_id
    assert client.get(url).json()['can_delete'] is False
    assert client.delete(url).status_code == 403
    # An accounting link does not grant deletion authority for Slack-created work.
    with store.connect() as conn:
        slack = store.slack_identity_in(conn, 'TTEAM', 'UOWNER')
    linked = settled_session(app, owner=slack)
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:other', slack))
    assert client.get('/api/runs/' + linked).json()['can_delete'] is False
    assert client.delete('/api/runs/' + linked).status_code == 403
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get(url).json()['can_delete'] is True
    assert client.delete(url, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.delete(url, headers={'Origin': 'https://other.example'}).status_code == 403
    store.execute("INSERT INTO model_requests(id,key_hash,run_id,user_id,model,created_at,status,cost) VALUES('retained-cost','key',?,'google:bob','model',?,'completed','0.5')", (run_id, now()))
    original_messages = store.messages(run_id)
    assert client.delete(url).status_code == 200
    assert client.delete(url).status_code == 200
    assert store.run(run_id)['deleted_at']
    assert store.messages(run_id) == original_messages
    assert store.rows("SELECT cost FROM model_requests WHERE id='retained-cost'")[0]['cost'] == '0.5'
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert client.delete('/api/runs/' + linked).status_code == 200
    client.cookies.clear()
    assert client.delete(url).status_code == 401


def test_delete_hides_parent_and_agents_and_blocks_all_session_access(workspace):
    from test_agent_sidebar import seeded_group
    from test_spend import sign_in
    from app.security import digest

    app, client = workspace
    store = app.state.store
    sign_in(app, client, 'alice', 'alice@berri.ai')
    parent, child, _ = seeded_group(app)
    for native_run in (parent, child):
        store.execute("INSERT INTO native_sessions(run_id,turn_id,actor_id,encrypted) VALUES(?,1,'google:alice','opaque')", (native_run,))
    folder = client.post('/api/session-folders', json={'name': 'Keep folder'}).json()['id']
    assert client.put('/api/runs/' + parent + '/folder', json={'folder_id': folder}).status_code == 200
    assert client.post('/api/runs/' + parent + '/archive', json={'archived': True}).status_code == 200
    side = settled_session(app, side_chat_of=parent)
    removed_side = settled_session(app, side_chat_of=side)
    assert client.delete('/api/runs/' + child).status_code == 422
    assert client.delete('/api/runs/' + removed_side).status_code == 200
    assert client.get('/api/runs/' + side + '/side-chats').json() == []
    assert client.delete('/api/runs/' + parent).status_code == 200
    assert store.run(child)['deleted_at'] == store.run(parent)['deleted_at']
    assert not store.rows('SELECT * FROM native_sessions WHERE run_id IN (?,?)', (parent, child))
    for archived in (False, True):
        rows = client.get('/api/runs', params={'scope': 'all', 'archived': archived, 'focus': child}).json()
        assert not {parent, child, removed_side}.intersection(row['id'] for row in rows)
    assert client.get('/api/session-folders').json()['folders'][0]['session_count'] == 0
    for run_id in (parent, child):
        url = '/api/runs/' + run_id
        for suffix in ('', '/side-chats', '/artifact', '/files', '/files/content?path=secret', '/computer', '/computer/captures/capture.png'):
            assert client.get(url + suffix).status_code == 404, suffix
        stream = client.get(url + '/events', headers={'Last-Event-ID': '1'})
        assert stream.status_code == 200
        assert stream.text == 'event: deleted\ndata: {}\n\n'  # No retained history on reconnect.
        assert client.post(url + '/messages', json={'content': 'Cannot continue', 'client_id': 'after-delete'}).status_code == 404
        assert client.post(url + '/cancel').status_code == 404
        assert client.post(url + '/archive', json={'archived': False}).status_code == 404
        assert client.put(url + '/folder', json={'folder_id': None}).status_code == 404
        assert client.put(url + '/title', json={'title': 'Cannot rename deleted session', 'expected_title': ''}).status_code == 404
        assert client.get('/api/credentials', params={'run_id': run_id}).status_code == 404
        with pytest.raises(ValueError, match='deleted'):
            store.enqueue_message(run_id, 'Cannot bypass HTTP', 'bypass-delete')
    assert client.post('/api/runs', json={'prompt': 'Cannot copy deleted context', 'side_chat_of': parent}).status_code == 404
    assert client.get('/api/runs/' + side).status_code == 200  # Side chats are independent sessions.
    # A stale worker cannot reuse an otherwise valid capability on retained rows.
    store.execute("UPDATE runs SET mode='modal',status='running',token_hash=? WHERE id=?", (digest('test-capability'), parent))
    assert client.post('/broker/' + parent + '/tools/call', json={'name': 'anything', 'arguments': {}}, headers={'Authorization': 'Bearer test-capability'}).status_code == 401


@pytest.mark.parametrize('busy', ['running', 'queued', 'injected', 'warm', 'warm_cleanup', 'slack_sending'])
def test_delete_automatically_stops_family_and_waits_for_receipts(workspace, busy):
    from test_agent_sidebar import seeded_group
    from app.db import now

    app, client = workspace
    store = app.state.store
    parent, child, _ = seeded_group(app)
    if busy == 'running':
        store.update_run(child, status='running')
    elif busy in {'queued', 'injected'}:
        store.execute('UPDATE messages SET status=? WHERE run_id=?', (busy, child))
    elif busy == 'slack_sending':
        async def begin_delivery():
            # Model the real owner's atomic claim. Background reconciliation
            # must not see a sending receipt before its in-memory owner exists.
            async with app.state.slack.chat.delivery_claim:
                store.execute("INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,status,created_at) VALUES(?,'sending','answer','Answer','sending',?)", (child, now()))
                receipt = store.rows("SELECT id FROM slack_outbox WHERE run_id=? AND dedupe_key='sending'", (child,))[0]['id']
                app.state.slack.chat.delivering.add(receipt)
                return receipt
        receipt = client.portal.call(begin_delivery)
    else:
        store.execute('CREATE TABLE IF NOT EXISTS durable_sessions(run_id TEXT PRIMARY KEY,state TEXT NOT NULL)')
        store.execute('INSERT INTO durable_sessions VALUES(?,?)', (child, json.dumps({'phase': busy})))
    response = client.delete('/api/runs/' + parent)
    assert response.status_code in {200, 202}
    if busy in {'warm', 'warm_cleanup', 'slack_sending'}:
        assert response.status_code == 202 and response.json()['deleting']
        assert store.run(parent)['deleted_at'] == store.run(child)['deleted_at'] == ''
        assert store.run(parent)['deletion_requested_at'] == store.run(child)['deletion_requested_at'] != ''
        assert client.get('/api/runs/' + parent).json()['status'] == 'deleting'
        warning = app.state.session_lifecycle.deletion_errors.get(parent, '')
        assert 'retry automatically' in warning
        for member in (parent, child):
            assert client.get('/api/runs/' + member).json()['deletion_error'] == warning
        listed = client.get('/api/runs', params={'scope': 'all'}).json()
        assert next(row for row in listed if row['id'] == parent)['deletion_error'] == warning
        for member in (parent, child):
            with pytest.raises(ValueError, match='deleted'):
                store.enqueue_message(member, 'Cannot start more work', 'during-delete')
        # The real runtime/delivery owners acknowledge cleanup later. No
        # second DELETE is required to complete the recorded request.
        if busy == 'slack_sending':
            store.execute("UPDATE slack_outbox SET status='sent' WHERE run_id=?", (child,))
            app.state.slack.chat.delivering.discard(receipt)
        else:
            store.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps({'phase': 'idle'}), child))
    wait_for(lambda: store.run(parent)['deleted_at'])
    assert store.run(child)['deleted_at'] == store.run(parent)['deleted_at']
    assert parent not in app.state.session_lifecycle.deletion_errors
    assert not store.rows("SELECT 1 FROM messages WHERE run_id IN (?,?) AND status IN ('queued','running','injected')", (parent, child))


def test_delete_waits_for_local_job_and_finishes_without_another_request(workspace):
    app, client = workspace
    app.state.settings.demo_step_seconds = 1.5
    run_id = client.post('/api/runs', json={'prompt': 'Delete active work', 'mode': 'demo'}).json()['id']
    wait_for(lambda: app.state.store.run(run_id)['status'] == 'running')
    response = client.delete('/api/runs/' + run_id)
    assert response.status_code == 202
    assert response.json()['deleting']
    assert app.state.store.run(run_id)['deleted_at'] == ''
    wait_for(lambda: app.state.store.run(run_id)['deleted_at'])
    assert not app.state.manager.is_active(run_id)
    assert client.get('/api/runs/' + run_id).status_code == 404


@pytest.mark.parametrize('missing', [False, True])
def test_delete_confirms_legacy_sandbox_cleanup_and_retries_failure(workspace, monkeypatch, missing):
    import modal
    from unittest.mock import AsyncMock

    app, client = workspace
    store, manager = app.state.store, app.state.manager
    run_id = settled_session(app)
    store.execute("UPDATE runs SET sandbox_id='sb-leftover',mode='modal' WHERE id=?", (run_id,))
    released = []
    async def terminate():
        released.append('terminated')
    sandbox = SimpleNamespace(object_id='sb-leftover', poll=aio(AsyncMock(return_value=None)),
                              terminate=aio(terminate), wait=aio(AsyncMock(return_value=0)))
    get = AsyncMock(side_effect=ConnectionError('private provider diagnostic'))
    monkeypatch.setattr(manager, 'provider', lambda **kwargs: SimpleNamespace(get=get))
    manager.computer.save_captures = AsyncMock()
    response = client.delete('/api/runs/' + run_id)
    assert response.status_code == 202
    assert 'private' not in response.text
    assert store.run(run_id)['deleted_at'] == '' and store.run(run_id)['deletion_requested_at']
    get.side_effect = modal.exception.NotFoundError('Gone') if missing else None
    get.return_value = sandbox
    wait_for(lambda: store.run(run_id)['deleted_at'], timeout=5)
    assert released == ([] if missing else ['terminated'])
    assert manager.computer.save_captures.await_count == (0 if missing else 1)
    assert client.delete('/api/runs/' + run_id).json()['deleted']


def test_pending_delete_restarts_from_saved_intent_without_new_confirmation(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
                        litellm_api_key='', modal_token_id='', modal_token_secret='', session_titles_enabled=False)
    original = create_app(settings)
    with TestClient(original, base_url=settings.public_url, client=('127.0.0.1', 50000)):
        run_id = settled_session(original)
        # Process loss after the admission transaction, before scheduling.
        original.state.session_lifecycle.request_delete(run_id, 'google:bob', False)
        with pytest.raises(ValueError, match='deleted'):
            original.state.store.enqueue_message(run_id, 'No revival', 'pending-delete')
    recovered = create_app(settings)
    with TestClient(recovered, base_url=settings.public_url, client=('127.0.0.1', 50000)) as client:
        wait_for(lambda: recovered.state.store.run(run_id)['deleted_at'])
        client.get('/api/session')
        assert client.get('/api/runs/' + run_id).status_code == 404


def test_delete_and_enqueue_share_transaction_boundary(workspace):
    from threading import Barrier
    from fastapi import HTTPException

    app, _ = workspace
    store, lifecycle = app.state.store, app.state.session_lifecycle
    outcomes = set()
    for index in range(12):
        run_id = settled_session(app)
        barrier = Barrier(2)
        def delete():
            barrier.wait()
            try:
                lifecycle.delete(run_id, 'google:bob', False)
                return 'deleted'
            except HTTPException as exc:
                assert exc.status_code == 409
                return 'busy'
        def enqueue():
            barrier.wait()
            try:
                store.enqueue_message(run_id, 'Concurrent follow-up', f'race-{index:04}')
                return 'queued'
            except ValueError as exc:
                assert 'deleted' in str(exc)
                return 'blocked'
        with ThreadPoolExecutor(2) as pool:
            deleted, queued = pool.submit(delete), pool.submit(enqueue)
            outcome = (deleted.result(), queued.result())
        assert outcome in {('deleted', 'blocked'), ('busy', 'queued')}
        assert bool(store.run(run_id)['deleted_at']) != store.has_queued_messages(run_id)
        outcomes.add(outcome)
    assert outcomes  # Scheduling may favor one side; both valid outcomes preserve the invariant.


async def test_open_event_stream_closes_when_session_is_deleted(workspace):
    app, client = workspace
    run_id = settled_session(app)
    async def connected():
        return False
    request = SimpleNamespace(headers={}, cookies=dict(client.cookies), is_disconnected=connected)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/runs/{run_id}/events')
    response = await endpoint(run_id, request)
    stream = response.body_iterator
    for _ in range(10):
        if 'event: run-status' in await anext(stream):
            break
    else:
        pytest.fail('The open session stream never published its status')
    lifecycle = app.state.session_lifecycle
    lifecycle.request_delete(run_id, 'google:bob', False)
    app.state.store.update_run(run_id, status='cancelled')
    lifecycle.deletion_errors[run_id] = 'Cleanup will retry automatically.'
    status = await anext(stream)
    assert 'event: run-status' in status
    assert json.loads(status.split('data: ')[1])['deletion_error'] == lifecycle.deletion_errors[run_id]
    app.state.session_lifecycle.delete(run_id, 'google:bob', False)
    assert await anext(stream) == 'event: deleted\ndata: {}\n\n'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_deleted_session_rejects_replayed_creation_and_side_chat_source(workspace):
    app, client = workspace
    store = app.state.store
    run_id = settled_session(app, client_id='create-before-delete')
    assert client.delete('/api/runs/' + run_id).status_code == 200
    with pytest.raises(ValueError, match='deleted'):
        store.create_run('Settled session', '', 'demo', [], chat_enabled=True, user_id='google:bob', client_id='create-before-delete')
    with pytest.raises(ValueError, match='no longer exists'):
        store.create_run('Side chat copy', '', 'demo', [], chat_enabled=True, side_chat_of=run_id)
    assert store.claim_message(run_id) is None
    assert app.state.session_lifecycle.can_delete({'owner_id': '', 'parent_run_id': ''}, '', False) is False


def activity_event(store, run_id, kind, message, data=None):
    from app.db import now
    with store.connect() as connection:
        return connection.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?) RETURNING id',
                                  (run_id, kind, message, json.dumps(data or {}), now())).fetchone()[0]


def test_chat_summary_defers_completed_bodies_and_keeps_live_activity(workspace):
    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Earlier request', '', 'demo', [], chat_enabled=True)['id']
    first = store.claim_message(run_id)['id']
    old = activity_event(store, run_id, 'tool', 'Long command output',
                         {'turn_id': first, 'output': 'historical-body-' * 10000})
    store.finish_message(run_id, first, 'Earlier answer')
    store.enqueue_message(run_id, 'Current request', 'current-activity')
    current = store.claim_message(run_id)['id']
    store.update_run(run_id, status='running')
    live = activity_event(store, run_id, 'tool', 'Current command', {'turn_id': current, 'phase': 'started'})
    full = client.get('/api/runs/' + run_id)
    summary = client.get('/api/runs/' + run_id, params={'activity': 'summary'})
    assert full.status_code == summary.status_code == 200
    data = summary.json()
    assert data['messages'] == full.json()['messages']
    assert data['loaded_activity'] == [str(current)]
    assert data['deferred_activity'] == [str(first)]
    assert old not in {event['id'] for event in data['events']}
    assert live in {event['id'] for event in data['events']}
    assert data['activity_cursor'] == live
    assert 'historical-body-' not in summary.text
    assert len(summary.content) < len(full.content) / 10
    assert old in {event['id'] for event in full.json()['events']}
    history = client.get(f'/api/runs/{run_id}/activity', params={'message_id': first}).json()
    assert old in {event['id'] for event in history['events']}
    store.finish_message(run_id, current, 'Current answer')
    store.update_run(run_id, status='idle')
    completed = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert completed['loaded_activity'] == []
    assert set(completed['deferred_activity']) == {str(first), str(current)}
    assert any(event['message'] == 'Response saved' and event['data']['message_id'] == current
               for event in completed['events'])


def test_activity_pages_are_bounded_and_keep_the_first_snapshot(workspace):
    from app.db import now

    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Paginated request', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)['id']
    with store.connect() as connection:
        connection.executemany('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
            [(run_id, 'tool', f'Command {index}', json.dumps({'turn_id': turn}), now()) for index in range(450)])
    live = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert live['loaded_activity'] == [str(turn)]
    assert len([event for event in live['events'] if event['kind'] == 'tool']) == 450
    store.finish_message(run_id, turn, 'Finished')
    store.update_run(run_id, status='idle')
    url = f'/api/runs/{run_id}/activity'
    first = client.get(url, params={'message_id': turn, 'until': 2**63-1}).json()
    expected = [event['id'] for event in store.events(run_id, limit=10000)
                if event['data'].get('turn_id') == turn or event['data'].get('message_id') == turn]
    assert len(first['events']) == 200 and first['has_more']
    assert first['until'] == expected[-1]
    late = activity_event(store, run_id, 'status', 'Late receipt', {'turn_id': turn})
    received, page = list(first['events']), first
    while page['has_more']:
        page = client.get(url, params={'message_id': turn, 'after': page['next_after'], 'until': first['until']}).json()
        assert len(page['events']) <= 200 and page['until'] == first['until']
        received.extend(page['events'])
    assert [event['id'] for event in received] == expected
    assert late not in {event['id'] for event in received}
    empty = client.get(url, params={'message_id': turn, 'after': first['until'], 'until': first['until']}).json()
    assert empty['events'] == [] and not empty['has_more']
    assert empty['next_after'] == first['until']
    latest = client.get(url, params={'message_id': turn, 'after': first['until']}).json()
    assert [event['id'] for event in latest['events']] == [late]


def test_activity_history_retains_recovery_epochs_steering_and_legacy_ownership(workspace):
    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Original request', '', 'demo', [], chat_enabled=True)['id']
    first = store.claim_message(run_id)['id']
    store.finish_message(run_id, first, 'Earlier result')
    second = store.enqueue_message(run_id, 'Other request', 'other-activity')[0]['id']
    steering = store.enqueue_message(run_id, 'Steering input', 'steering-activity')[0]['id']
    store.execute("UPDATE messages SET steering_parent_id=?,status='injected' WHERE id=?", (first, steering))
    store.execute('DELETE FROM events WHERE run_id=?', (run_id,))
    def record(kind, message, data=None):
        return activity_event(store, run_id, kind, message, data)

    record('status', 'Before any response')
    record('chat', 'Response started', {'message_id': first})
    owned = [record('tool', 'Legacy before recovery')]
    record('chat', 'Response started', {'message_id': first})
    owned.append(record('tool', 'Legacy after repeated start'))
    record('chat', 'Response started', {'message_id': second})
    other = [record('tool', 'Other turn legacy activity')]
    record('chat', 'Response saved', {'message_id': first})
    other.append(record('tool', 'Other turn after earlier late save'))
    owned.append(record('tool', 'Interleaved tagged activity', {'turn_id': first}))
    record('chat', 'Response started', {'message_id': first})
    owned.append(record('tool', 'Legacy recovered activity', {'turn_id': 0}))
    record('chat', 'Response saved', {'message_id': first})
    record('tool', 'Legacy outside a response')
    owned.append(record('tool', 'Tagged final receipt', {'turn_id': first}))
    root = client.get(f'/api/runs/{run_id}/activity', params={'message_id': first}).json()
    child = client.get(f'/api/runs/{run_id}/activity', params={'message_id': steering}).json()
    assert child == root
    assert [event['id'] for event in root['events'] if event['kind'] == 'tool'] == owned
    other_page = client.get(f'/api/runs/{run_id}/activity', params={'message_id': second}).json()
    assert [event['id'] for event in other_page['events'] if event['kind'] == 'tool'] == other
    store.execute("UPDATE messages SET status='running' WHERE id=?", (first,))
    summary = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert set(summary['loaded_activity']) == {str(first), str(second)}
    assert summary['deferred_activity'] == []
    assert {event['id'] for event in summary['events'] if event['kind'] == 'tool'} == set(owned + other)


def test_activity_snapshot_cannot_skip_events_committed_during_chat_read(workspace, monkeypatch):
    import app.activity_history as activity_history

    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Snapshot request', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)['id']
    store.update_run(run_id, status='running')
    before = activity_event(store, run_id, 'tool', 'Before snapshot', {'turn_id': turn})
    original = store.messages
    saved = []

    def finish_after_run_read(selected, *, connection=None):
        # WAL permits the independent writer to commit while the endpoint holds
        # its read snapshot. Message, lifecycle and cursor must stay consistent.
        if selected == run_id and connection is not None and not saved:
            saved.append(activity_event(store, run_id, 'tool', 'Committed during read', {'turn_id': turn}))
            store.finish_message(run_id, turn, 'Concurrent answer')
            store.update_run(run_id, status='idle')
        return original(selected, connection=connection)

    monkeypatch.setattr(store, 'messages', finish_after_run_read)
    summary = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert summary['activity_cursor'] == before
    assert summary['loaded_activity'] == [str(turn)]
    assert next(message for message in summary['messages'] if message['id'] == turn)['status'] == 'running'
    assert saved[0] not in {event['id'] for event in summary['events']}
    assert saved[0] in {event['id'] for event in store.events(run_id, after=summary['activity_cursor'])}
    assert not any(event['message'] == 'Response saved' for event in summary['events'])
    fresh = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert fresh['deferred_activity'] == [str(turn)] and fresh['loaded_activity'] == []
    assert any(message['content'] == 'Concurrent answer' for message in fresh['messages'])
    # A second writer at the marker read cannot advance the summary cursor.
    original_markers = activity_history.markers
    additional = []

    def append_after_cursor(connection, selected, cursor):
        if selected == run_id and not additional:
            additional.append(activity_event(store, run_id, 'status', 'After cursor', {'turn_id': turn}))
        return original_markers(connection, selected, cursor)

    monkeypatch.setattr(activity_history, 'markers', append_after_cursor)
    final = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert final['activity_cursor'] < additional[0]
    assert additional[0] in {event['id'] for event in store.events(run_id, after=final['activity_cursor'])}


def test_activity_endpoint_shares_auth_deletion_and_message_boundaries(workspace):
    app, client = workspace
    store = app.state.store
    run_id = store.create_run('History access', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)['id']
    store.finish_message(run_id, turn, 'Finished')
    store.update_run(run_id, status='idle')
    assistant = next(message['id'] for message in store.messages(run_id) if message['role'] == 'assistant')
    foreign = store.create_run('Other session', '', 'demo', [], chat_enabled=True)['id']
    foreign_turn = store.messages(foreign)[0]['id']
    url = f'/api/runs/{run_id}/activity'
    for message in (assistant, foreign_turn, 9999999):
        assert client.get(url, params={'message_id': message}).status_code == 404
    store.execute('UPDATE messages SET steering_parent_id=? WHERE id=?', (foreign_turn, turn))
    assert client.get(url, params={'message_id': turn}).status_code == 404
    store.execute('UPDATE messages SET steering_parent_id=NULL WHERE id=?', (turn,))
    for params in ({'message_id': 0}, {'message_id': turn, 'after': -1},
                   {'message_id': turn, 'until': 2**63}, {'message_id': 2**63}):
        assert client.get(url, params=params).status_code == 422
    assert client.get(url, params={'message_id': turn}).status_code == 200
    assert client.delete('/api/runs/' + run_id).status_code == 200
    assert client.get(url, params={'message_id': turn}).status_code == 404
    assert client.get('/api/runs/' + run_id, params={'activity': 'summary'}).status_code == 404
    client.cookies.clear()
    assert client.get(f'/api/runs/{foreign}/activity', params={'message_id': foreign_turn}).status_code == 401
    assert client.get('/api/runs/' + foreign, params={'activity': 'summary'}).status_code == 401


@pytest.mark.parametrize('status', ['failed', 'interrupted', 'cancelled', 'steered', 'save_failed', 'awaiting_approval', 'missing_answer'])
def test_chat_summary_keeps_commentary_until_a_successful_answer(workspace, status):
    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Unfinished request', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)['id']
    store.update_run(run_id, status='running')
    update = activity_event(store, run_id, 'message', 'Here is the progress so far.',
                            {'turn_id': turn, 'public_update': True})
    tool = activity_event(store, run_id, 'tool', 'Recorded work', {'turn_id': turn})
    if status == 'missing_answer':
        store.execute("UPDATE messages SET status='completed' WHERE id=?", (turn,))
        activity_event(store, run_id, 'chat', 'Response saved', {'message_id': turn})
        store.update_run(run_id, status='idle')
    elif status == 'awaiting_approval':
        store.update_run(run_id, status=status)
    else:
        store.finish_message(run_id, turn, 'The request still needs attention.', status=status)
        store.update_run(run_id, status=status)
    summary = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert summary['deferred_activity'] == []
    assert summary['loaded_activity'] == [str(turn)]
    assert {update, tool} <= {event['id'] for event in summary['events']}
    assert next(event for event in summary['events'] if event['id'] == update)['data']['public_update'] is True


def test_chat_summary_matches_successful_answers_around_inline_session_id(workspace):
    from app.db import now
    from app.session_metadata import complete_in

    app, client = workspace
    store = app.state.store
    run_id = store.create_run('Main request', '', 'demo', [], chat_enabled=True)['id']
    turn = store.claim_message(run_id)['id']
    old = activity_event(store, run_id, 'tool', 'Main request history', {'turn_id': turn})
    inline = store.enqueue_message(run_id, 'What is the session ID?', 'inline-activity')[0]['id']
    with store.connect() as connection:
        complete_in(connection, run_id, inline, now())
    live = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert live['deferred_activity'] == [] and live['loaded_activity'] == [str(turn)]
    assert old in {event['id'] for event in live['events']}
    store.finish_message(run_id, turn, 'Main request completed')
    store.update_run(run_id, status='idle')
    summary = client.get('/api/runs/' + run_id, params={'activity': 'summary'}).json()
    assert summary['deferred_activity'] == [str(turn)] and summary['loaded_activity'] == []
    assert old not in {event['id'] for event in summary['events']}
