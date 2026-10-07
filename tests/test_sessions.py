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
    assert not [m for m in reopened.messages(run['id']) if m['role'] == 'assistant']
    from app.runner import RunManager
    recovery = RunManager(reopened, runner.settings)
    recovery.settings = runner.settings.model_copy(update={'modal_token_id': ''})
    await recovery.recover()
    await recovery.recover()
    answers = [m for m in reopened.messages(run['id']) if m['role'] == 'assistant']
    assert len(answers) == 1 and answers[0]['content'].startswith('Tests passed')
    assert answers[0]['status'] == 'save_failed'
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
    assert not [m for m in runner.store.messages(run['id']) if m['role'] == 'assistant']
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


@pytest.mark.parametrize('busy', ['running', 'queued', 'injected', 'manager', 'warm', 'warm_cleanup', 'slack_sending'])
def test_delete_rejects_unsettled_parent_or_child(workspace, monkeypatch, busy):
    from test_agent_sidebar import seeded_group
    from app.db import now

    app, client = workspace
    store = app.state.store
    parent, child, _ = seeded_group(app)
    if busy == 'running':
        store.update_run(child, status='running')
    elif busy in {'queued', 'injected'}:
        store.execute('UPDATE messages SET status=? WHERE run_id=?', (busy, child))
    elif busy == 'manager':
        monkeypatch.setattr(app.state.manager, 'is_active', lambda run_id: run_id == child)
    elif busy == 'slack_sending':
        store.execute("INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,status,created_at) VALUES(?,'sending','answer','Answer','sending',?)", (child, now()))
    else:
        store.execute('CREATE TABLE IF NOT EXISTS durable_sessions(run_id TEXT PRIMARY KEY,state TEXT NOT NULL)')
        store.execute('INSERT INTO durable_sessions VALUES(?,?)', (child, json.dumps({'phase': busy})))
    response = client.delete('/api/runs/' + parent)
    assert response.status_code == 409 and 'Stop' in response.json()['detail']
    assert store.run(parent)['deleted_at'] == store.run(child)['deleted_at'] == ''


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
