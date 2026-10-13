import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.computer_operations import ComputerOperations
from app.durable_runner import initialize_schema
from agent.tools import mcp_bridge, remote_computer
from test_codex_sdk import codex_agent
from test_session_journal import journal
from test_durable import durable, drive
from test_shared_environment import project_checkout, project_spec


@pytest.fixture
def operations(journal):
    store, caller, _, _ = journal
    initialize_schema(store)
    store.execute('INSERT INTO durable_sessions(run_id,state) VALUES(?,?)',
                  (caller['id'], json.dumps({'independent_runtime': True, 'phase': 'monitor'})))
    def submit(conn, run):
        conn.execute('UPDATE durable_sessions SET revision=revision+1 WHERE run_id=?', (run['id'],))
    return store, caller, ComputerOperations(store, submit)


def request(**changes):
    return {'action': 'computer_call', 'request_id': uuid4().hex, 'name': 'computer_terminal',
            'arguments': {'command': 'publish authorized change'}, **changes}


def test_broker_computer_admission_uses_the_durable_controller(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from test_spend import active
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, temporal_enabled=True,
                             independent_agent_runtime=True))
    try:
        # Deliberately omit lifespan: no Temporal or cloud service is started.
        client = TestClient(app, base_url=app.state.settings.public_url, client=('127.0.0.1', 50000))
        caller = active(app)
        app.state.store.execute('INSERT INTO durable_sessions(run_id,state) VALUES(?,?)',
            (caller['id'], json.dumps({'independent_runtime': True, 'phase': 'monitor'})))
        body = request(name='computer_write_file', arguments={'path': 'receipt', 'text': 'result'})
        for _ in range(2):
            response = client.post('/broker/'+caller['id']+'/agent-runtime', json=body,
                                   headers={'Authorization': 'Bearer capability'})
            assert response.status_code == 200 and response.json()['status'] == 'queued', response.text
        rows = app.state.store.rows('SELECT * FROM computer_operations')
        assert len(rows) == 1 and rows[0]['machine_id'] == ''
        assert app.state.store.rows('SELECT revision FROM durable_sessions')[0]['revision'] == 1
        app.state.store.update_run(caller['id'], status='stopping')
        assert client.post('/broker/'+caller['id']+'/agent-runtime', json=body,
                           headers={'Authorization': 'Bearer capability'}).status_code == 401
    finally:
        app.state.store.close()


def test_operation_identity_survives_lost_ack_and_controller_restart(operations):
    store, caller, service = operations
    body = request()
    assert service.exchange(caller, body)['status'] == 'queued'
    recovered = ComputerOperations(store, service.submit)
    assert recovered.exchange(caller, body)['status'] == 'queued'
    row = recovered.pending(caller['id'])
    recovered.dispatch(caller, row, 'computer-one')
    recovered.dispatch(caller, row, 'computer-one')
    with pytest.raises(HTTPException, match='another computer'):
        recovered.dispatch(caller, row, 'computer-two')
    receipt = {'content': [{'type': 'text', 'text': 'verified receipt'}], 'isError': False}
    assert recovered.settle(row, 'completed', receipt)
    assert recovered.exchange(caller, body) == {'request_id': body['request_id'], 'status': 'completed', 'result': receipt}
    assert recovered.pending(caller['id']) is None
    assert store.rows('SELECT revision FROM durable_sessions')[0]['revision'] == 1
    with pytest.raises(HTTPException, match='another operation'):
        recovered.exchange(caller, {**body, 'arguments': {'command': 'different action'}})


@pytest.mark.parametrize('change', [{'active_user_id': 'another-user'}, {'active_message_id': 98765},
                                    {'token_hash': 'rotated'}, {'status': 'stopping'}])
def test_owner_change_fences_new_dispatch_but_retains_late_receipts(operations, change):
    store, caller, service = operations
    body = request()
    service.exchange(caller, body)
    row = service.pending(caller['id'])
    service.dispatch(caller, row, 'computer')
    key, value = next(iter(change.items()))
    store.execute('UPDATE runs SET '+key+'=? WHERE id=?', (value, caller['id']))
    with pytest.raises(HTTPException, match='owner changed'):
        service.dispatch(caller, row, 'computer')
    with pytest.raises(HTTPException, match='owner changed'):
        service.exchange(caller, body)
    with pytest.raises(HTTPException, match='owner changed'):
        service.read(caller, {})
    assert service.settle(row, 'unknown', {'instruction': 'inspect effects'})
    assert service.settle(row, 'completed', {'receipt': 'Late verified output'})
    assert not service.settle(row, 'unknown', {'instruction': 'stale polling result'})


def test_operation_receipts_remain_completely_enumerable(operations):
    _, caller, service = operations
    expected = [request(name='computer_read_file', arguments={'path': str(i)}) for i in range(55)]
    for body in expected:
        service.exchange(caller, body)
    found, cursor = [], 0
    while True:
        page = service.read(caller, {'after': cursor, 'limit': 7})
        found += [row['request_id'] for row in page['operations']]
        if page['next'] is None:
            break
        cursor = page['next']
    assert found == [body['request_id'] for body in expected]


def test_computer_bridge_keeps_one_identity_through_provisioning(monkeypatch):
    monkeypatch.setenv('MOYAI_REMOTE_COMPUTER', '1')
    monkeypatch.setattr(remote_computer.time, 'sleep', lambda _: None)
    calls = []
    receipt = {'content': [{'type': 'text', 'text': 'command handle'}], 'isError': False}
    def broker(route, body):
        calls.append(body)
        assert route == '/agent-runtime'
        return {'status': 'queued' if len(calls) == 1 else 'completed', 'result': receipt}
    monkeypatch.setattr(mcp_bridge, 'broker', broker)
    monkeypatch.setattr(mcp_bridge.computer_tools, 'call', lambda *a: pytest.fail('Generated command executed in the control guest'))
    assert mcp_bridge.call_tool({'name': 'computer_terminal', 'arguments': {'command': 'echo result'}}, []) == receipt
    assert calls[0] == calls[1]


def test_lost_computer_reply_preserves_a_reconciliation_identity(monkeypatch):
    def lost(*args):
        raise OSError('connection lost after dispatch')
    result = remote_computer.call('computer_terminal', {'command': 'publish'}, lost)
    value = json.loads(result['content'][0]['text'])
    assert result['isError'] and value['outcome'] == 'unknown' and len(value['request_id']) == 32


def test_full_history_reference_read_does_not_acquire_computer(tmp_path, monkeypatch):
    from agent.context_views import history_view
    view = history_view([{'role': 'user', 'content': 'Complete requirement '*10000}], directory=tmp_path, budget=3000)
    path = next(tmp_path.glob('*.jsonl'))
    monkeypatch.setenv('MOYAI_HISTORY_REFERENCE_DIR', str(tmp_path))
    monkeypatch.setenv('MOYAI_REMOTE_COMPUTER', '1')
    monkeypatch.setattr(mcp_bridge, 'broker', lambda *a: pytest.fail('Computer acquired to read a public history reference'))
    result = mcp_bridge.call_tool({'name': 'computer_read_file', 'arguments': {'path': str(path), 'length': path.stat().st_size}}, [])
    assert not result['isError'] and json.loads(result['content'][0]['text'])['text'] == path.read_text()
    assert str(path) in view[0]['content']
    path.unlink(); path.symlink_to(tmp_path/'private-file')
    assert mcp_bridge.call_tool({'name': 'computer_read_file', 'arguments': {'path': str(path)}}, [])['isError']


def test_selected_agent_enters_shared_lifecycle_without_computer_preparation(tmp_path, monkeypatch):
    from sandbox import agent_runtime, agent
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'scoped-capability')
    def forbidden(*args, **kwargs):
        pytest.fail('Computer preparation blocked first inference')
    for name in ('prepare_attachments', 'prepare_project', 'github_checkout', 'computer_request'):
        monkeypatch.setattr(agent, name, forbidden)
    spec = {'run_id': 'session', 'harness': 'codex', 'model': 'openai/gpt-6-astra',
            'prompt': 'Does this API exist?', 'independent_runtime': True, 'repo_url': 'https://github.com/org/repo'}
    def shared(actual, **kwargs):
        assert actual == spec
        assert kwargs['workspace'] == tmp_path/'control'
        env = kwargs['config']['mcp_servers']['workspace']['env']
        assert env['MOYAI_REMOTE_COMPUTER'] == '1' and env['MOYAI_WORKSPACE'] == '/workspace'
        kwargs['on_input']('new-input')
        return {'exit_code': 0}
    monkeypatch.setattr(agent_runtime, 'run_conversation', shared)
    assert agent_runtime.run(spec, SimpleNamespace(url='http://relay.test'), lambda *a: None,
                             control=tmp_path/'control', session=tmp_path/'session') == 0
    assert agent.ACTIVITY_INPUT_ID == 'new-input'


def test_independent_codex_configuration_disables_native_workspace_effects(codex_agent, tmp_path):
    import tomllib
    agent, _, _ = codex_agent
    agent.context.spec['independent_runtime'] = True
    config = agent.sdk_config(tmp_path/'native-home')
    settings = tomllib.loads('\n'.join(config.config_overrides))
    assert settings['mcp_servers']['moyai']['default_tools_approval_mode'] == 'approve'
    for name in ('shell_tool', 'unified_exec', 'browser_use', 'browser_use_external', 'computer_use', 'image_generation', 'multi_agent'):
        assert settings['features'][name] is False


@pytest.fixture
def runtime_durable(durable):
    manager, cloud, run_id = durable
    manager.settings.independent_agent_runtime = True
    manager.store.execute("UPDATE runs SET harness='codex' WHERE id=?", (run_id,))
    return manager, cloud, run_id


async def test_answer_settles_with_computer_provisioning_unavailable(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    manager.settings.max_concurrent_runs = 1
    async def unavailable(*args, **kwargs):
        pytest.fail('A full computer was acquired before answering')
    monkeypatch.setattr(manager, 'provision', unavailable)
    await drive(manager, run_id)
    assert len(cloud.machines) == 1 and cloud.machines[0].name.startswith('moyai-runtime-')
    assert cloud.machines[0].spec['independent_runtime'] and cloud.machines[0].spec['harness'] == 'codex'
    assert cloud.machines[0].spec['model'] == 'test-model'
    assert cloud.snapshots == 0 and manager.store.run(run_id)['sandbox_id'] == ''
    assert manager.store.messages(run_id)[0]['status'] == 'completed'
    assert manager.has_capacity()


async def test_later_computer_call_uses_same_turn_then_checkpoints_only_computer(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    original_owner = manager.store.run(run_id)
    assert manager.has_capacity() and not state.get('computer')
    body = request(arguments={'command': 'make the requested file'})
    manager.agent_runtime.operations.exchange(original_owner, body)
    original_command = cloud.command
    async def command(machine, action, directory, value, **kwargs):
        if '/computer-operations/' in directory and action == 'read':
            result = ({'workspace': '/workspace'} if machine.spec.get('prepare_computer') else
                      {'saved': True} if machine.spec.get('finish_computer') else
                      {'content': [{'type': 'text', 'text': 'command-handle'}], 'isError': False})
            return json.dumps({'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 0,
                               'final': {'tool_result': result}})
        return await original_command(machine, action, directory, value, **kwargs)
    monkeypatch.setattr(manager, 'command', command)
    for _ in range(4):
        async with manager.session_guard(run_id):
            state = manager.state(run_id)
            await manager.agent_runtime.computer_step(run_id, state)
    result = manager.agent_runtime.operations.read(original_owner, {'request_id': body['request_id']})
    assert result['status'] == 'completed'
    current = manager.store.run(run_id)
    assert (current['active_message_id'], current['active_user_id'], current['token_hash']) == (
        original_owner['active_message_id'], original_owner['active_user_id'], original_owner['token_hash'])
    assert current['sandbox_id'] == cloud.machines[1].object_id
    assert len(cloud.machines) == 2
    await drive(manager, run_id)
    assert cloud.snapshots == 1 and all(not machine.alive for machine in cloud.machines)
    assert manager.store.run(run_id)['snapshot_id'] == 'im-1'


@pytest.mark.parametrize('accepted', [False, True])
def test_computer_http_rejection_does_not_hide_a_prior_dispatch(monkeypatch, accepted):
    from urllib.error import HTTPError
    calls = []
    def broker(route, body):
        calls.append(body)
        if accepted and len(calls) == 1:
            return {'status': 'queued'}
        raise HTTPError('http://broker', 403, 'Revoked', {}, None)
    monkeypatch.setattr(remote_computer.time, 'sleep', lambda _: None)
    result = remote_computer.call('computer_write_file', {'path': 'file', 'text': 'result'}, broker)
    body = json.loads(result['content'][0]['text'])
    assert body['outcome'] == ('unknown' if accepted else 'rejected')
    assert body['dispatched'] is (None if accepted else False)


def test_computer_launch_freezes_spec_before_lost_ack_retry(tmp_path, monkeypatch):
    from sandbox.durable_process import start
    directory, source = tmp_path/'job', tmp_path/'spec.json'
    source.write_text(json.dumps({'name': 'original'}))
    calls = []
    monkeypatch.setattr('sandbox.durable_process.subprocess.Popen', lambda args, **kw: calls.append(args))
    start(directory, source)
    source.write_text(json.dumps({'name': 'later'}))
    start(directory, source)
    assert all(json.loads(Path(args[-1]).read_text()) == {'name': 'original'} for args in calls)


def test_remote_commands_activate_project_without_restarting_services(tmp_path, project_checkout, monkeypatch):
    from sandbox import remote_tool, project_environment
    from agent.tools import computer_tools
    root = project_checkout
    (root/'.venv/bin').mkdir(parents=True)
    monkeypatch.setattr(remote_tool, 'WORKSPACE_ROOT', root.parent)
    # Map only the known project location, preserving normal path behavior.
    monkeypatch.setattr(project_environment, 'Path', lambda value: root if value == '/workspace/repo' else Path(value))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'scoped-capability')
    monkeypatch.setenv('PATH', os.environ['PATH'])
    monkeypatch.setattr(remote_tool, 'BrokerRelay', lambda *a: SimpleNamespace(
        start=lambda: SimpleNamespace(url='http://relay', close=lambda: None)))
    monkeypatch.setattr('sandbox.attachments.prepare_attachments', lambda *a: None)
    run, probes = subprocess.run, []

    def command(arguments, **kwargs):
        assert arguments[0] == 'git', 'Services restarted for a tool'
        probes.append(arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(project_environment.subprocess, 'run', command)
    monkeypatch.setattr(mcp_bridge, 'BROKER', '')
    monkeypatch.setattr(mcp_bridge, 'GIT_BROKER', '')
    monkeypatch.setattr(mcp_bridge, 'TOKEN', '')
    monkeypatch.setenv('MOYAI_CREDENTIAL_PROXY_URL', '')
    monkeypatch.setenv('MOYAI_WORKSPACE', '')
    monkeypatch.chdir(tmp_path)
    def execute(params, _, *, relay):
        assert relay.url == 'http://relay'
        assert os.getcwd() == str(root.parent)
        assert str(root/'.venv/bin') in os.environ['PATH'].split(os.pathsep)
        assert os.environ['MOYAI_CREDENTIAL_PROXY_URL'] == 'http://relay/credentials'
        return {'content': [], 'isError': False}
    monkeypatch.setattr(remote_tool, 'call_tool', execute)
    assert remote_tool.run({**project_spec(startup='must not run'), 'workspace': str(root),
                           'name': 'computer_terminal', 'arguments': {}}) == 0
    assert len(probes) == 2


@pytest.fixture
def remote_workspace(project_checkout, monkeypatch, capsys):
    from sandbox import remote_tool
    monkeypatch.setattr(remote_tool, 'WORKSPACE_ROOT', project_checkout.parent)
    monkeypatch.setattr(remote_tool, 'BrokerRelay', lambda *args: SimpleNamespace(
        start=lambda: SimpleNamespace(url='http://relay', close=lambda: None)))
    monkeypatch.setattr('sandbox.attachments.prepare_attachments', lambda *args: None)
    for field in ('BROKER', 'GIT_BROKER', 'TOKEN'):
        monkeypatch.setattr(mcp_bridge, field, '')
    for field in ('MOYAI_CREDENTIAL_PROXY_URL', 'MOYAI_WORKSPACE'):
        monkeypatch.setenv(field, '')

    def invoke(**fields):
        assert remote_tool.run({**project_spec(), 'workspace': str(project_checkout), **fields}) == 0
        events = [json.loads(line.removeprefix('WORKSPACE_EVENT ')) for line in capsys.readouterr().out.splitlines()
                  if line.startswith('WORKSPACE_EVENT ')]
        return events[-1]['tool_result']

    return invoke


@pytest.mark.parametrize('condition', ['healthy', 'missing', 'empty', 'wrong'])
def test_sibling_checkout_guidance_and_files_share_the_computer_boundary(
        project_checkout, remote_workspace, condition):
    root = project_checkout
    sibling = root.parent / 'second-repository'
    sibling.mkdir()
    (sibling / 'AGENTS.md').write_text('Preserve this repository guidance.')
    (sibling / 'local-work.txt').write_text('Keep sibling changes.')
    if condition != 'healthy':
        root.rename(root.parent / 'preserved-project')
        if condition in {'empty', 'wrong'}:
            root.mkdir()
        if condition == 'wrong':
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/other/repo.git'],
                           check=True, capture_output=True)
    guidance = remote_workspace(name='repository_guidance', arguments={'paths': [str(sibling)]})
    assert not guidance['isError']
    data = json.loads(guidance['content'][0]['text'])
    assert data['paths'][0]['guidance'][0]['content'] == 'Preserve this repository guidance.'
    result = remote_workspace(name='computer_read_file', arguments={'path': str(sibling / 'local-work.txt')})
    assert not result['isError'] and json.loads(result['content'][0]['text'])['text'] == 'Keep sibling changes.'
    denied = remote_workspace(name='repository_guidance', arguments={'paths': [str(root.parent.parent)]})
    assert denied['isError']


def test_remote_checkpoint_saves_sibling_files_with_legacy_checkout_scope(
        project_checkout, remote_workspace, monkeypatch):
    import zipfile
    root = project_checkout
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                    'commit', '--allow-empty', '-m', 'fixture'], check=True, capture_output=True)
    sibling = root.parent / 'second-repository'
    sibling.mkdir()
    (sibling / 'local-work.txt').write_text('Keep sibling changes.')
    monkeypatch.setattr('sandbox.computer.request', lambda *args, **kwargs: {})
    assert remote_workspace(finish_computer=True, response='Saved') == {'saved': True}
    with zipfile.ZipFile(root.parent.parent / 'artifacts/result.zip') as archive:
        assert archive.read('new-files/second-repository/local-work.txt') == b'Keep sibling changes.'
        assert archive.read('new-files/repo/uncommitted.txt') == b'Keep this unfinished work.'


@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('tool', ['computer_read_file', 'computer_write_file', 'computer_terminal'])
def test_moved_remote_cwd_falls_back_without_recreating_checkout(project_checkout, remote_workspace, prepared, tool):
    root = project_checkout
    preserved = root.parent / 'preserved-project'
    root.rename(preserved)
    arguments = {
        'computer_read_file': {'path': 'preserved-project/uncommitted.txt'},
        'computer_write_file': {'path': 'preserved-project/repair.txt', 'text': 'Repair allowed.'},
        'computer_terminal': {'command': 'pwd; cat preserved-project/uncommitted.txt', 'yield_ms': 3000},
    }[tool]
    result = remote_workspace(name=tool, arguments=arguments,
                              **({} if prepared else {'project_environment': {}}))
    assert not result['isError']
    data = json.loads(result['content'][0]['text'])
    assert os.environ['MOYAI_WORKSPACE'] == os.getcwd() == str(root.parent)
    assert not root.exists()
    if tool == 'computer_read_file':
        assert data['text'] == 'Keep this unfinished work.'
    elif tool == 'computer_write_file':
        assert (preserved / 'repair.txt').read_text() == 'Repair allowed.'
    else:
        assert data['status'] == 'completed' and data['exit_code'] == 0
        assert str(root.parent) in data['output'] and 'Keep this unfinished work.' in data['output']
    if prepared:
        assert result['project_health'] == {'status': 'needs_repair', 'phase': 'root', 'error_type': 'MissingProjectRoot'}
        assert json.loads(result['content'][-1]['text'])['project_health'] == result['project_health']
    else:
        assert 'project_health' not in result


@pytest.mark.parametrize('replacement', [False, True])
def test_terminal_repairs_restored_root_and_retains_uncommitted_files(project_checkout, remote_workspace, replacement):
    root = project_checkout
    (root / '.moyai-ready').touch()
    root.rename(root.parent / 'preserved-project')
    if replacement:
        root.mkdir()
    result = remote_workspace(name='computer_terminal', arguments={'command':
        f'cd "{root.parent}" && if [ -d repo ]; then rmdir repo; fi && mv preserved-project repo '
        '&& test -f repo/.moyai-ready && cat repo/uncommitted.txt', 'yield_ms': 3000},
        project_health={'status': 'ready', 'phase': 'verify'})
    assert not result['isError']
    data = json.loads(result['content'][0]['text'])
    assert data['status'] == 'completed' and data['exit_code'] == 0
    assert data['output'] == 'Keep this unfinished work.'
    assert result['project_health']['status'] == 'needs_repair'
    assert (root / 'uncommitted.txt').read_text() == 'Keep this unfinished work.'
    assert not (root.parent / 'preserved-project').exists()


@pytest.mark.parametrize('arguments', [{'path': '../escape', 'text': 'no'},
                                        {'path': 'bad', 'text': 1}, {'path': 'bad', 'unexpected': True}])
def test_repair_health_does_not_bypass_file_validation(project_checkout, remote_workspace, arguments):
    root = project_checkout
    root.rename(root.parent / 'preserved-project')
    result = remote_workspace(name='computer_write_file', arguments=arguments)
    assert result['isError'] and result['project_health']['status'] == 'needs_repair'
    assert not (root.parent / 'bad').exists() and not (root.parent.parent / 'escape').exists()


@pytest.mark.parametrize('authorized', [False, True])
def test_repair_health_keeps_github_catalog_authorization(project_checkout, remote_workspace, monkeypatch, authorized):
    root = project_checkout
    root.rename(root.parent / 'preserved-project')
    calls = []
    monkeypatch.setattr(mcp_bridge, 'broker', lambda route, body=None:
                        [{'name': 'github_checkout'}] if authorized else [])
    monkeypatch.setattr(mcp_bridge.github_tools, 'call', lambda *args, **kwargs: calls.append(args) or {'directory': 'second'})
    result = remote_workspace(name='github_checkout', arguments={'repository': 'org/second', 'directory': 'second'})
    assert result['isError'] is not authorized
    assert len(calls) == int(authorized)
    assert not root.exists()


def test_remote_prepare_returns_health_without_rejecting_computer(project_checkout, remote_workspace):
    project_checkout.rename(project_checkout.parent / 'preserved-project')
    result = remote_workspace(prepare_computer=True)
    assert result['workspace'] == str(project_checkout.parent) and 'error' not in result
    assert result['project_health']['status'] == 'needs_repair'


@pytest.mark.parametrize('late_root_failure', [False, True])
@pytest.mark.parametrize('health_event_failure', [False, True])
async def test_repair_health_survives_controller_restart_and_dispatches_once(
        runtime_durable, monkeypatch, late_root_failure, health_event_failure):
    from app.independent_runtime import IndependentRuntime
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    caller = manager.store.run(run_id)
    body = request(arguments={'command': 'inspect and repair'})
    service = manager.agent_runtime.operations
    service.exchange(caller, body)
    health = {'status': 'needs_repair', 'phase': 'verify', 'error_type': 'CalledProcessError', 'exit_code': 23}
    prepared_health = health
    if late_root_failure:
        prepared_health = {'status': 'ready', 'phase': 'verify'}
        health = {'status': 'needs_repair', 'phase': 'root', 'error_type': 'MissingProjectRoot'}
    command = cloud.command
    specs = []

    async def reports(machine, action, directory, value, **kwargs):
        response = await command(machine, action, directory, value, **kwargs)
        if '/computer-operations/' in directory and action == 'read':
            specs.append(dict(machine.spec))
            if machine.spec.get('prepare_computer'):
                result = {'workspace': '/workspace/repo', 'project_health': prepared_health}
            else:
                assert machine.spec['project_health'] == prepared_health
                result = {'content': [{'type': 'text', 'text': 'Repair receipt'}], 'isError': False, 'project_health': health}
            return json.dumps({'state': 'done', 'final': {'tool_result': result}})
        return response

    monkeypatch.setattr(manager, 'command', reports)
    event = manager.store.event

    def health_event(*args, **kwargs):
        if health_event_failure and len(args) > 3 and isinstance(args[3], dict) and args[3].get('project_health'):
            raise RuntimeError('Health activity storage unavailable')
        return event(*args, **kwargs)

    monkeypatch.setattr(manager.store, 'event', health_event)
    for _ in range(4):
        manager.agent_runtime = IndependentRuntime(manager)
        state = manager.state(run_id)
        await manager.agent_runtime.computer_step(run_id, state)
    result = manager.agent_runtime.operations.read(caller, {'request_id': body['request_id']})
    assert result['status'] == 'completed' and result['result']['project_health'] == health
    state = manager.state(run_id)
    assert state['computer']['phase'] == 'ready' and state['computer']['project_health'] == health
    assert len(specs) == 2 and len([path for path in cloud.launches if '/computer-operations/' in path]) == 2
    events = manager.store.rows("SELECT data FROM events WHERE run_id=? AND kind='status'", (run_id,))
    assert any(json.loads(event['data']).get('project_health') == health for event in events) is not health_event_failure
    current = manager.store.run(run_id)
    for key in ('active_message_id', 'active_user_id', 'token_hash', 'snapshot_id', 'repo_url', 'environment_build_id'):
        assert current[key] == caller[key]


@pytest.mark.parametrize('report', [
    {'state': 'uncertain'},
    {'state': 'done', 'final': {}},
    {'state': 'done', 'final': {'tool_result': {'error': 'Preparation rejected'}}},
])
async def test_unconfirmed_preparation_never_becomes_repair_ready(runtime_durable, monkeypatch, report):
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    state['computer'] = {'phase': 'prepare', 'sandbox_id': cloud.machines[0].object_id}

    async def command(*args, **kwargs):
        return json.dumps(report)

    monkeypatch.setattr(manager, 'command', command)
    with pytest.raises(HTTPException, match='preparation|Preparation|supervisor|verified'):
        await manager.agent_runtime.ensure_computer(run_id, state)
    assert state['computer']['phase'] == 'prepare' and 'project_health' not in state['computer']


async def test_runtime_restart_recovers_one_launch_and_stop_settles_pending(runtime_durable):
    from app.independent_runtime import IndependentRuntime
    manager, cloud, run_id = runtime_durable
    cloud.lose_launch_ack = True
    with pytest.raises(ConnectionError):
        await drive(manager, run_id)
    manager.agent_runtime = IndependentRuntime(manager)
    await drive(manager, run_id, phase='monitor')
    caller = manager.store.run(run_id)
    manager.agent_runtime.operations.exchange(caller, request())
    manager.store.update_run(run_id, status='stopping')
    await drive(manager, run_id)
    assert len(cloud.launches) == 1 and len(cloud.machines) == 1
    assert not cloud.machines[0].alive and manager.has_capacity()
    assert manager.store.rows('SELECT status FROM computer_operations') == [{'status': 'cancelled'}]


async def test_lost_computer_retains_runtime_and_never_redispatches(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    caller = manager.store.run(run_id)
    body = request()
    service = manager.agent_runtime.operations
    service.exchange(caller, body)
    # Model a verified dispatch on a computer that disappeared before its receipt.
    service.dispatch(caller, service.pending(run_id), 'sb-lost')
    state['computer'] = {'phase': 'ready', 'sandbox_id': 'sb-lost', 'workspace': '/workspace'}
    manager.save(run_id, state)
    from app.durable_runner import LostExecution
    async def unavailable(state):
        raise LostExecution()
    monkeypatch.setattr(manager, 'sandbox', unavailable)
    await manager.agent_runtime.computer_step(run_id, state)
    assert state['computer'] == {} and state['computer_generation'] == 1
    assert service.read(caller, {'request_id': body['request_id']})['status'] == 'unknown'
    assert cloud.machines[0].alive
    assert manager.has_capacity() and manager.store.run(run_id)['sandbox_id'] == ''


async def test_ui_wake_is_durable_and_uses_shared_computer_capacity(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    monkeypatch.setattr(type(manager.settings), 'missing_sandbox', lambda *a: [])
    assert manager.computer_state(run_id)['wake_action'] == 'acquire'
    await manager.wake_computer(run_id)
    await manager.wake_computer(run_id)
    state = manager.state(run_id)
    assert state['computer_requested'] and manager.computer_state(run_id)['waking']
    monkeypatch.setattr(manager, 'has_capacity', lambda *a: False)
    async def full(*args, **kwargs):
        return False
    monkeypatch.setattr(manager, 'make_capacity', full)
    await manager.agent_runtime.computer_step(run_id, state)
    assert len(cloud.machines) == 1 and not state['computer']
    assert manager.store.run(run_id)['active_message_id'] == state['message_id']


async def test_each_computer_generation_and_checkpoint_segment_gets_fresh_jobs(runtime_durable, monkeypatch):
    manager, cloud, run_id = runtime_durable
    await drive(manager, run_id, phase='monitor')
    state = manager.state(run_id)
    paths = []
    async def command(machine, action, directory, value, **kwargs):
        if action == 'start':
            paths.append(directory)
        return json.dumps({'state': 'running'})
    monkeypatch.setattr(manager, 'command', command)
    for segment, generation in [(0, 0), (1, 0), (1, 1)]:
        state.update(segment=segment, computer_generation=generation)
        await manager.agent_runtime.job(run_id, state, cloud.machines[0], 'prepare', {'prepare_computer': True})
    assert len(set(paths)) == 3
