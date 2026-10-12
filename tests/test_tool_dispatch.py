"""Real broker/SQL dispatch ordering; only the external side effect is a fixture."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from app.connector_errors import ConnectorError
from app.message_queue import MessageQueue
from app.tool_dispatch import ToolDispatch
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app  # noqa: F401
from test_workspace import cloud_capability, workspace  # noqa: F401


@pytest.fixture
def broker(durable_app, active, monkeypatch):
    manager, store, scope = active
    store.execute('UPDATE runs SET plugins=? WHERE id=?', (json.dumps(['github', 'notion']), scope.run_id))
    monkeypatch.setattr(durable_app.state.connectors, 'allowed', lambda *a, **kw: True)
    calls = []

    async def connected(*args, **kwargs):
        calls.append(args)
        return {'id': 'confirmed-fixture-write'}

    monkeypatch.setattr(durable_app.state.connectors, 'call', connected)
    monkeypatch.setattr(durable_app.state.connectors.github, 'call', connected)
    return durable_app, manager, store, scope, calls


def body(scope, *, name='notion_append', identity='a' * 32):
    arguments = ({'repository': 'BerriAI/moyai', 'request_key': 'fixture-write', 'title': 'Fixture'}
                 if name == 'github_create_issue' else {'page_id': 'b' * 32, 'text': 'Fixture write'})
    return {'name': name, 'arguments': arguments, 'input_id': scope.message_id, 'request_id': identity}


def correction(manager, store, scope, *, accept=True):
    message, _ = store.enqueue_message(scope.run_id, 'Cancel the obsolete write.', 'correction',
                                      user_id='owner', send_immediately=True)
    if accept:
        manager.message_queue.live_control(scope.run_id, scope.message_id, [])
        manager.message_queue.acknowledge(scope.run_id, scope.message_id, [message['id']])
    return message['id']


def receipts(store, scope):
    return [json.loads(row['data']) for row in store.rows(
        "SELECT data FROM events WHERE run_id=? AND kind='tool_dispatch' ORDER BY id", (scope.run_id,))]


@pytest.mark.parametrize('accept', [False, True])
async def test_correction_during_connection_refresh_cancels_unstarted_write(broker, monkeypatch, accept):
    app, manager, store, scope, calls = broker
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        entered.set()
        await release.wait()

    monkeypatch.setattr(app.state.connectors.github, 'ensure_connection', refresh)
    task = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call',
        body(scope, name='github_create_issue')))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        correction(manager, store, scope, accept=accept)
    finally:
        release.set()
    result = await task
    assert calls == []
    assert result['outcome'] == 'cancelled' and result['dispatched'] is False
    assert receipts(store, scope) == []


@pytest.mark.parametrize('transition', ['accepted', 'pending', 'stop', 'actor', 'model'])
@pytest.mark.parametrize('internal', [False, True])
async def test_input_and_execution_changes_before_claim_cancel_every_tool_family(broker, monkeypatch, transition, internal):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    claim = ToolDispatch.claim

    def delayed(self):
        entered.set()
        assert release.wait(5)
        return claim(self)

    monkeypatch.setattr(ToolDispatch, 'claim', delayed)
    monkeypatch.setattr(app.state.skills, 'call', lambda *a: calls.append(a) or {'content': 'obsolete'})
    request = body(scope)
    if internal:
        request.update(name='skills_search', arguments={'query': 'fixture'})
    task = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', request))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if transition in {'accepted', 'pending'}:
            correction(manager, store, scope, accept=transition == 'accepted')
        else:
            field, value = {'stop': ('status', 'stopping'), 'actor': ('active_user_id', 'another-user'),
                            'model': ('active_model', 'another-model')}[transition]
            store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, scope.run_id))
    finally:
        release.set()
    result = await task
    assert result['outcome'] == 'cancelled' and result['dispatched'] is False
    assert calls == [] and receipts(store, scope) == []


async def test_dispatch_transaction_serializes_with_acknowledgment(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    original = MessageQueue.current_input

    def checked(conn, *args):
        valid = original(conn, *args)
        entered.set()
        assert release.wait(5)
        return valid

    monkeypatch.setattr(MessageQueue, 'current_input', staticmethod(checked))
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    acknowledging = None
    attempted = threading.Event()

    def acknowledge():
        attempted.set()
        return correction(manager, store, scope)
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        acknowledging = asyncio.create_task(asyncio.to_thread(acknowledge))
        assert await asyncio.to_thread(attempted.wait, 3)
        await asyncio.sleep(.05)
        assert not acknowledging.done(), 'Input cannot commit between the dispatch check and receipt'
    finally:
        release.set()
    await sending
    await acknowledging
    assert len(calls) == 1
    assert receipts(store, scope)[0]['outcome'] == 'completed'


async def test_running_write_keeps_receipt_after_correction_without_replay(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = asyncio.Event(), asyncio.Event()

    async def connected(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()
        return {'id': 'confirmed-fixture-write'}

    monkeypatch.setattr(app.state.connectors, 'call', connected)
    request = body(scope)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert receipts(store, scope)[0]['outcome'] == 'unknown'
        duplicate = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
        assert duplicate['outcome'] == 'unknown' and duplicate['retryable'] is False
        correction(manager, store, scope)
        assert not sending.done()
    finally:
        release.set()
    assert (await sending)['id'] == 'confirmed-fixture-write'
    # A fresh handler after replacement observes the same receipt, even after steering.
    repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    assert repeated['outcome'] == 'completed' and repeated['retryable'] is False
    assert len(calls) == 1 and len(receipts(store, scope)) == 1


async def test_cancelled_delivery_of_confirmed_receipt_preserves_completed_outcome(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered, release = threading.Event(), threading.Event()
    finish = ToolDispatch.finish

    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        finish(self, *args, **kwargs)

    monkeypatch.setattr(ToolDispatch, 'finish', held)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        sending.cancel()
        await asyncio.sleep(.02)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await sending
    # A disconnected HTTP client can finish before the server drains its write.
    async with asyncio.timeout(3):
        while receipts(store, scope)[0]['outcome'] != 'completed':
            await asyncio.sleep(.01)
    assert receipts(store, scope)[0]['outcome'] == 'completed' and len(calls) == 1


async def test_cancelled_running_write_stays_uncertain_and_cannot_be_replayed(broker, monkeypatch):
    app, manager, store, scope, calls = broker
    entered = asyncio.Event()

    async def uncertain(*args, **kwargs):
        calls.append(args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.connectors, 'call', uncertain)
    sending = asyncio.create_task(manager.deepagents_runtime.broker(scope, '/tools/call', body(scope)))
    await asyncio.wait_for(entered.wait(), 3)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    result = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    assert result['outcome'] == 'unknown' and result['retryable'] is False
    assert len(calls) == 1


async def test_request_identity_cannot_be_rebound_or_omitted(broker):
    app, manager, store, scope, calls = broker
    request = body(scope)
    await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    request['arguments']['text'] = 'A different write'
    with pytest.raises(ApplicationError, match='409'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    for field in ('input_id', 'request_id'):
        with pytest.raises(ApplicationError, match='422'):
            await manager.deepagents_runtime.broker(scope, '/tools/call', {k: v for k, v in request.items() if k != field})
    assert len(calls) == 1


@pytest.mark.parametrize('dispatched', [False, True])
async def test_duplicate_preserves_known_unsent_or_uncertain_provider_outcome(broker, monkeypatch, dispatched):
    app, manager, store, scope, calls = broker

    async def failed(*args, **kwargs):
        calls.append(args)
        raise ConnectorError('Fixture transport failure', dispatched=dispatched)

    monkeypatch.setattr(app.state.connectors, 'call', failed)
    original = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    duplicate = await manager.deepagents_runtime.broker(scope, '/tools/call', body(scope))
    assert original['outcome'] == duplicate['outcome'] == ('unknown' if dispatched else 'rejected')
    assert original['dispatched'] == duplicate['dispatched'] == dispatched
    assert duplicate['outcome_uncertain'] == dispatched
    assert len(calls) == 1


@pytest.mark.parametrize('steered', [False, True])
async def test_computer_artifact_fetch_preserves_nested_dispatch_scope(broker, tmp_path, monkeypatch, steered):
    from agent.tools import mcp_bridge
    from sandbox import remote_tool
    from test_published_artifacts import save
    app, manager, store, scope, _ = broker
    raw = b'original saved bytes' * 70000
    artifact = save(app.state.published_artifacts, store.run(scope.run_id), raw)
    loop = asyncio.get_running_loop()

    def send(path, body=None):
        return asyncio.run_coroutine_threadsafe(manager.deepagents_runtime.broker(scope, path, body), loop).result(10)

    monkeypatch.setattr(mcp_bridge, '_broker', send)
    monkeypatch.setattr(remote_tool, 'BrokerRelay', lambda *a: SimpleNamespace(
        start=lambda: SimpleNamespace(url='http://fixture-relay', close=lambda: None)))
    monkeypatch.setattr('sandbox.attachments.prepare_attachments', lambda *a: None)
    monkeypatch.setattr('sandbox.project_environment.activate_project', lambda *a: None)
    for field in ('BROKER', 'GIT_BROKER', 'TOKEN'):
        monkeypatch.setattr(mcp_bridge, field, '')
    for field in ('WORKSPACE_RUN_TOKEN', 'MOYAI_WORKSPACE', 'MOYAI_CREDENTIAL_PROXY_URL'):
        monkeypatch.setenv(field, 'fixture')
    monkeypatch.chdir(tmp_path)
    if steered:
        correction(manager, store, scope)
    assert await asyncio.to_thread(remote_tool.run, {'workspace': str(tmp_path), 'broker_url': 'http://fixture',
        'broker_tool_scope': {'input_id': scope.message_id, 'request_id': 'c' * 32},
        'name': 'artifact_fetch', 'arguments': {'path': artifact['path'], 'revision': artifact['revision'],
                                              'destination': 'copy.bin'}}) == 0
    if steered:
        assert not (tmp_path / 'copy.bin').exists()
        assert receipts(store, scope) == []
    else:
        assert (tmp_path / 'copy.bin').read_bytes() == raw
        saved = receipts(store, scope)
        assert len(saved) > 1 and len({r['request_id'] for r in saved}) == len(saved)
        assert all(r['input_id'] == scope.message_id and r['outcome'] == 'completed' for r in saved)


async def test_computer_job_keeps_admitted_input_after_restart_and_steering(broker, monkeypatch):
    from app.computer_operations import ComputerOperations
    app, manager, store, scope, _ = broker
    state = {**manager.state(scope.run_id), 'independent_runtime': True}
    manager.save(scope.run_id, state)
    service = manager.agent_runtime.operations
    request = {'action': 'computer_call', 'request_id': 'd' * 32, 'name': 'artifact_fetch',
               'arguments': {'path': 'saved.bin', 'destination': 'copy.bin'}, 'input_id': scope.message_id}
    caller = store.run(scope.run_id)
    service.exchange(caller, request)
    service.dispatch(caller, service.pending(scope.run_id), 'fixture-computer')
    manager.agent_runtime.operations = ComputerOperations(store, manager.submit_in)
    correction_id = correction(manager, store, scope)
    specs, directories = [], []

    async def write(value, path):
        specs.append(json.loads(value))

    async def command(sandbox, action, directory, *args, **kwargs):
        directories.append(directory)
        return json.dumps({'state': 'done', 'final': {'tool_result': {'workspace': '/workspace'}}})

    monkeypatch.setattr(manager, 'command', command)
    machine = SimpleNamespace(filesystem=SimpleNamespace(write_text=SimpleNamespace(aio=write)))
    await manager.agent_runtime.job(scope.run_id, state, machine, request['request_id'],
                                   {'name': request['name'], 'arguments': request['arguments']})
    assert specs[-1]['broker_tool_scope'] == {'input_id': scope.message_id, 'request_id': request['request_id']}
    # Preparation belongs to the current request and gets a new immutable job
    # after a correction, instead of replaying an old cancelled preparation.
    await manager.agent_runtime.job(scope.run_id, state, machine, 'prepare', {'prepare_computer': True})
    assert specs[-1]['broker_tool_scope']['input_id'] == correction_id
    assert f'prepare-{correction_id}' in directories[-1]


async def test_steering_waits_for_old_preparation_to_end_before_restarting(broker, monkeypatch):
    app, manager, store, scope, _ = broker
    runtime = manager.agent_runtime
    state = {**manager.state(scope.run_id), 'independent_runtime': True,
             'computer': {'phase': 'prepare', 'sandbox_id': 'fixture-computer'}}
    manager.save(scope.run_id, state)
    specs, starts = [], []
    old_finished = False

    async def write(value, path):
        specs.append(json.loads(value))

    machine = SimpleNamespace(filesystem=SimpleNamespace(write_text=SimpleNamespace(aio=write)))

    async def sandbox(computer):
        return machine

    async def command(sandbox, action, directory, *args, **kwargs):
        if action == 'start':
            starts.append(directory)
        if f'prepare-{scope.message_id}' in directory:
            result = ({'state': 'done', 'final': {'tool_result': {'error': 'Old input was cancelled.'}}}
                      if old_finished else {'state': 'running'})
        else:
            assert old_finished, 'Never overlap preparation jobs in the same workspace'
            result = {'state': 'done', 'final': {'tool_result': {'workspace': '/workspace'}}}
        return json.dumps(result)

    monkeypatch.setattr(manager, 'sandbox', sandbox)
    monkeypatch.setattr(manager, 'command', command)
    assert not await runtime.ensure_computer(scope.run_id, state)
    correction_id = correction(manager, store, scope)
    assert not await runtime.ensure_computer(scope.run_id, state)
    assert len(set(starts)) == 1 and specs[-1]['broker_tool_scope']['input_id'] == scope.message_id
    old_finished = True
    assert not await runtime.ensure_computer(scope.run_id, state)
    assert 'preparation_input_id' not in state['computer']
    assert await runtime.ensure_computer(scope.run_id, state)
    assert specs[-1]['broker_tool_scope']['input_id'] == correction_id
    assert len(set(starts)) == 2 and state['computer']['phase'] == 'ready'


def test_nested_request_ids_are_stable_and_assigned_before_chunking(monkeypatch):
    from sandbox import broker_transport
    scope = {'input_id': 7, 'request_id': 'e' * 32}
    requests = []
    monkeypatch.setattr(broker_transport, 'MAX_BODY', 512)
    monkeypatch.setattr(broker_transport, 'body_limit', lambda path: 256)
    send = lambda route, body: requests.append((route, body))
    invoke = broker_transport.scoped_tools(lambda path, body: broker_transport.send_payload(path, body, send), scope)
    invoke('/tools/call', {'name': 'github_create_pull_request', 'arguments': {'body': 'large' * 200}})
    import base64
    restored = json.loads(b''.join(base64.b64decode(body['data']) for path, body in requests if path == '/payloads'))
    assert restored['input_id'] == 7 and len(restored['request_id']) == 32
    assert set(requests[-1][1]) == {'name', 'moyai_payload'}
    original = restored['request_id']
    direct = []
    replay = broker_transport.scoped_tools(lambda path, body: direct.append(body), scope)
    replay('/tools/call', {'name': 'github_create_pull_request', 'arguments': {'body': 'large' * 200}})
    assert direct[0]['request_id'] == original


async def test_repository_preparation_uses_the_same_scoped_broker(broker, tmp_path, monkeypatch):
    import io
    from pathlib import Path
    from sandbox import agent
    app, manager, store, scope, calls = broker
    loop = asyncio.get_running_loop()
    async def refresh():
        return None
    monkeypatch.setattr(app.state.connectors.github, 'ensure_connection', refresh)
    monkeypatch.setattr(agent, 'Path', lambda value: tmp_path / value.lstrip('/'))
    monkeypatch.setattr(agent, 'prepare_attachments', lambda *a, **kw: None)
    monkeypatch.setattr(agent, 'prepare_project', lambda *a: None)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'fixture')
    def checkout(send, remote, token, **kwargs):
        return send('/tools/call', {'name': 'github_checkout', 'arguments': kwargs})
    monkeypatch.setattr(agent, 'github_checkout', checkout)
    def upstream(request, timeout):
        value = asyncio.run_coroutine_threadsafe(manager.deepagents_runtime.broker(scope,
            '/tools/call', json.loads(request.data)), loop).result(10)
        return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr(agent.urllib.request, 'urlopen', upstream)
    workspace, _ = await asyncio.to_thread(agent.prepare_workspace,
        {'repo_url': 'https://github.com/BerriAI/moyai', 'github_enabled': True, 'broker_url': 'http://fixture',
         'broker_tool_scope': {'input_id': scope.message_id, 'request_id': 'f' * 32}},
        SimpleNamespace(url='http://fixture', startup_failure=None))
    assert workspace == Path(tmp_path / 'workspace/repo')
    assert len(calls) == 1
    assert receipts(store, scope)[0]['tool'] == 'github_checkout'


@pytest.mark.parametrize('name', ['skills_save', 'memory_save', 'media_share', 'model_switch',
    'automation_create', 'credentials_request', 'agents_fanout', 'unknown_tool'])
async def test_local_argument_rejections_are_never_recorded_as_dispatched(broker, name):
    app, manager, store, scope, calls = broker
    request = {**body(scope), 'name': name, 'arguments': {}}
    try:
        result = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    except ApplicationError:
        pass  # Preserve existing HTTP error contracts as well as tool receipts.
    else:
        assert result.get('error') or result.get('isError'), result
    saved = receipts(store, scope)
    assert all(r['outcome'] == 'rejected' and r['dispatched'] is False for r in saved), saved
    if saved:
        repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
        assert repeated['dispatched'] is False and not repeated['outcome_uncertain']
        assert 'already dispatched' not in repeated['error']
    assert calls == []


@pytest.mark.parametrize('name', ['agents_fanout', 'credentials_request'])
async def test_unavailable_tools_never_claim_dispatch(broker, monkeypatch, name):
    app, manager, store, scope, calls = broker
    monkeypatch.setattr('app.agents.AgentCoordinator.available', lambda *a: False)
    monkeypatch.setattr(app.state.credentials, 'tools', lambda *a: [])
    with pytest.raises(ApplicationError, match='403'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', {**body(scope), 'name': name, 'arguments': {}})
    assert receipts(store, scope) == [] and calls == []


@pytest.mark.parametrize('arguments,status', [({'after': -1}, 422), ({'limit': 0}, 422),
    ({'after': True}, 422), ({'unexpected': True}, 422), ({'request_id': []}, 422),
    ({'request_id': ''}, 422), ({'request_id': 'b' * 32}, 404)])
async def test_computer_receipt_read_rejections_stay_unsent(broker, arguments, status):
    app, manager, store, scope, calls = broker
    request = {**body(scope), 'name': 'computer_operation_read', 'arguments': arguments}
    with pytest.raises(ApplicationError, match=str(status)):
        await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    saved = receipts(store, scope)
    assert len(saved) == 1 and saved[0]['outcome'] == 'rejected' and not saved[0]['dispatched']
    repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    assert repeated['outcome'] == 'rejected' and not repeated['dispatched']
    assert not repeated['outcome_uncertain'] and calls == []
    corrected = {**request, 'request_id': 'c' * 32, 'arguments': {}}
    assert await manager.deepagents_runtime.broker(scope, '/tools/call', corrected) == {
        'operations': [], 'next': None}
    assert receipts(store, scope)[-1]['outcome'] == 'completed'


async def test_computer_receipt_read_scope_rejection_stays_unsent(broker, monkeypatch):
    from app.computer_operations import ComputerOperations
    app, manager, store, scope, calls = broker
    original = ComputerOperations.current

    def changed(conn, caller):
        store.update_run(scope.run_id, status='stopping')
        return original(conn, caller)

    monkeypatch.setattr(ComputerOperations, 'current', staticmethod(changed))
    with pytest.raises(ApplicationError, match='409'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', {
            **body(scope), 'name': 'computer_operation_read', 'arguments': {}})
    saved = receipts(store, scope)
    assert len(saved) == 1 and saved[0]['outcome'] == 'rejected' and not saved[0]['dispatched']
    assert calls == []


async def test_unexpected_computer_receipt_failure_is_not_misclassified(broker, monkeypatch):
    from fastapi import HTTPException
    app, manager, store, scope, calls = broker

    def failed(*args):
        raise HTTPException(503, 'Receipt storage unavailable.')

    monkeypatch.setattr(manager.agent_runtime.operations, 'read', failed)
    with pytest.raises(ApplicationError, match='503'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', {
            **body(scope), 'name': 'computer_operation_read', 'arguments': {}})
    assert receipts(store, scope)[0]['outcome'] == 'unknown'


def test_unavailable_computer_receipt_service_never_claims_dispatch(workspace):
    app, client = workspace
    run_id, headers = cloud_capability(app, [])
    app.state.store.execute('UPDATE runs SET harness=? WHERE id=?', ('deepagents-temporal', run_id))
    assert not hasattr(app.state.manager, 'agent_runtime')
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers, json={
        'name': 'computer_operation_read', 'arguments': {}, 'input_id': 1, 'request_id': 'a' * 32})
    assert response.status_code == 409, response.text
    assert 'unavailable' in response.json()['detail']
    assert app.state.store.rows("SELECT 1 FROM events WHERE run_id=? AND kind='tool_dispatch'", (run_id,)) == []


async def test_error_after_internal_write_remains_uncertain(broker, monkeypatch):
    from fastapi import HTTPException
    app, manager, store, scope, calls = broker

    async def changed_after_write(*args):
        calls.append('counted internal write')
        # Automation can recheck current actor after committing and synchronizing.
        raise HTTPException(409, 'The requester changed after the saved action.')

    monkeypatch.setattr(app.state.automation_tools, 'call', changed_after_write)
    request = {**body(scope), 'name': 'automation_create', 'arguments': {}}
    with pytest.raises(ApplicationError, match='409'):
        await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    assert repeated['outcome'] == 'unknown' and repeated['dispatched'] is True
    assert repeated['outcome_uncertain'] is True and repeated['retryable'] is False
    assert calls == ['counted internal write']


@pytest.mark.parametrize('attribute,name', [('skills', 'skills_save'), ('memory', 'memory_save'),
                                          ('media_shares', 'media_share')])
async def test_definite_local_access_rejection_preserves_unsent_receipt(broker, monkeypatch, attribute, name):
    from fastapi import HTTPException
    app, manager, store, scope, calls = broker

    def denied(*args):
        raise HTTPException(403, 'Fixture access denial before mutation.')

    monkeypatch.setattr(getattr(app.state, attribute), 'call', denied)
    request = {**body(scope), 'name': name, 'arguments': {}}
    first = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    repeated = await manager.deepagents_runtime.broker(scope, '/tools/call', request)
    assert first['dispatched'] is repeated['dispatched'] is False
    assert first['outcome'] == repeated['outcome'] == 'rejected'
    assert not repeated['outcome_uncertain'] and calls == []
