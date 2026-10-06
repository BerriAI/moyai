"""Transient bootstrap outages recover without resubmitting model/tool writes."""
from contextlib import contextmanager
from io import BytesIO
import http.client
import json
from types import SimpleNamespace
import sys
import urllib.error
import urllib.request

import pytest

from app.db import Store
from app.temporal_runtime import TemporalRunManager
from sandbox import agent
from sandbox.startup import StartupUnavailable, read_with_reconnect
from test_durable import durable, drive  # noqa: F401
from test_workspace import workspace, cloud_capability  # noqa: F401


class Outage:
    def __init__(self, results):
        self.results, self.now, self.calls = iter(results), 0, 0
        self.notifications = []

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += seconds

    @contextmanager
    def open(self, request, timeout):
        assert request.get_method() == 'GET'
        assert 0 < timeout <= 10
        self.calls += 1
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        yield BytesIO(result)

    def read(self, budget=10):
        return read_with_reconnect(urllib.request.Request('https://workspace.example/tools'),
            lambda response: response.read(), stage='workspace_tools', budget=budget,
            opener=self.open, clock=lambda: self.now, sleep=self.sleep, notify=self.notifications.append)


def error(code):
    return urllib.error.HTTPError('https://workspace.example/tools', code, 'private body', {}, BytesIO(b'private'))


def test_bootstrap_waits_through_http_and_connection_outage():
    fault = Outage([error(503), http.client.RemoteDisconnected(), b'{"tools":[]}'])
    assert fault.read() == b'{"tools":[]}'
    assert fault.calls == 3 and fault.now == 3
    assert len(fault.notifications) == 2
    assert 'reconnected' in fault.notifications[-1]


@pytest.mark.parametrize('code', [401, 403, 404, 422])
def test_permanent_bootstrap_errors_are_not_retried(code):
    fault = Outage([error(code)])
    with pytest.raises(urllib.error.HTTPError):
        fault.read()
    assert fault.calls == 1 and not fault.notifications


def test_exhaustion_returns_only_safe_typed_diagnostics():
    fault = Outage([error(502)] * 10)
    with pytest.raises(StartupUnavailable) as result:
        fault.read(budget=4)
    assert result.value.stage == 'workspace_tools' and result.value.reason == 'HTTP 502'
    assert 'private' not in str(result.value)
    assert fault.calls == 3 and fault.now == 4


def test_partial_read_retries_but_posts_are_never_accepted():
    fault = Outage([b'one', b'two'])
    def read(response):
        if fault.calls == 1:
            raise http.client.IncompleteRead(b'partial')
        return response.read()
    assert read_with_reconnect(urllib.request.Request('https://example.test'), read,
        stage='attachments', opener=fault.open, clock=lambda: fault.now, sleep=fault.sleep) == b'two'
    with pytest.raises(ValueError, match='read-only'):
        read_with_reconnect(urllib.request.Request('https://example.test', data=b'{}'), read,
                            stage='workspace_tools', opener=fault.open)
    assert fault.calls == 2


def test_reconnecting_capability_still_works_and_stop_revokes_it(workspace):
    app, client = workspace
    run_id, headers = cloud_capability(app, [])
    app.state.store.update_run(run_id, status='reconnecting')
    assert client.get(f'/broker/{run_id}/tools', headers=headers).status_code == 200
    assert client.get(f'/broker/{run_id}/v1/models', headers=headers).status_code == 200
    assert client.get(f'/broker/{run_id}/tools', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    app.state.store.update_run(run_id, status='stopping')
    assert client.get(f'/broker/{run_id}/tools', headers=headers).status_code == 401


def test_agent_startup_marker_precedes_any_inference(tmp_path, monkeypatch):
    events, calls, workspace_tools = [], [], []
    class FakeAgent:
        tools = []
        valid_tool_names = []
        def __init__(self, **kwargs: object) -> None:
            callback = kwargs['interim_assistant_callback']
            assert callable(callback)
            self.commentary = callback
        def close(self): calls.append('closed')
        def run_conversation(self, *args, **kwargs):
            assert any(e[2].get('phase') == 'execution_started' for e in events)
            instructions = kwargs['system_message']
            assert 'before the first tool call' in instructions
            assert 'Do not send only a status tag as the opening' in instructions
            assert 'Before delegating to agents' in instructions
            calls.append('inference')
            self.commentary('<status>Auditing UI and schema changes</status>I’m checking the UI and schema before making changes.')
            self.commentary('<status>Verifying the corrected behavior</status>One public milestone.',
                            already_streamed=False)
            return {'final_response': '<status>Finishing the task</status>Done',
                    'messages': [], 'completed': True}
    relay = SimpleNamespace(url='http://loopback', startup_failure=StartupUnavailable('workspace_tools', 'HTTP 503'),
                            close=lambda: None, control=lambda body=None: {}, last_error='', wait_group='', wait_credential='')
    relay.start = lambda: relay
    monkeypatch.setattr(agent, 'BrokerRelay', lambda *args, **kwargs: relay)
    monkeypatch.setattr(agent, 'prepare_attachments', lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, 'collect_archive', lambda *args: None)
    monkeypatch.setattr(agent, 'Path', lambda value: tmp_path / str(value).lstrip('/'))
    monkeypatch.setattr(agent, 'emit', lambda kind, message, data=None, **extra: events.append((kind, message, data or {}, extra)))
    monkeypatch.setitem(sys.modules, 'run_agent', SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setitem(sys.modules, 'tools.mcp_tool_discovery', SimpleNamespace(discover_mcp_tools=lambda **kwargs: []))
    def definitions(**kwargs):
        assert kwargs == {'enabled_toolsets': ['mcp-workspace'], 'quiet_mode': True,
                          'skip_tool_search_assembly': True}
        return workspace_tools
    monkeypatch.setitem(sys.modules, 'model_tools', SimpleNamespace(get_tool_definitions=definitions))
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'test-token')
    monkeypatch.setenv('HERMES_HOME', '/home')
    monkeypatch.chdir(tmp_path)
    spec = {'run_id':'startup-test','broker_url':'https://example.test','repo_url':'','model':'test','max_iterations':0,'timeout':None,'prompt':'Question'}
    assert agent.run(spec) == 75
    assert calls == ['closed']
    assert events[-1][3]['startup_retry'] == {'version':1,'stage':'workspace_tools','reason':'HTTP 503'}
    assert not any(e[2].get('phase') == 'execution_started' for e in events)
    relay.startup_failure = None
    with pytest.raises(RuntimeError, match='MCP tools were not loaded'):
        agent.run(spec)
    workspace_tools.append({'function': {'name':'mcp_workspace_browser_open'}})
    with pytest.raises(RuntimeError, match='tool discovery was not enabled'):
        agent.run(spec)
    FakeAgent.valid_tool_names = {'tool_search', 'tool_describe', 'tool_call'}
    FakeAgent.tools = [{'function': {'name': name}} for name in FakeAgent.valid_tool_names]
    events.clear()
    agent.run(spec)
    assert 'inference' in calls
    assert not any(e[3].get('startup_retry') for e in events)
    assert [(kind, message) for kind, message, data, _ in events if data.get('phase') == 'focus'] == [
        ('status', 'Auditing UI and schema changes'), ('status', 'Verifying the corrected behavior')]
    assert [message for kind, message, _, _ in events if kind == 'message'] == [
        'I’m checking the UI and schema before making changes.', 'One public milestone.']
    assert [message for kind, message, _, _ in events if kind == 'final'] == ['Done']
    assert (tmp_path / 'artifacts/result.md').read_text() == 'Done'


def startup_report(*, events=None, exit_code=75):
    return {'state':'done','events':events or [],'cursor':len(events or []),'exit_code':exit_code,
            'final':{'kind':'final','message':'Reconnecting','completed':False,
                     'startup_retry':{'version':1,'stage':'workspace_tools','reason':'HTTP 503'}}}


async def fail_startup_once(manager, cloud, run_id, *, report=None):
    if manager.state(run_id).get('phase') != 'monitor':
        await drive(manager, run_id, phase='monitor')
    original = cloud.command
    failed_directory = manager.directory(manager.state(run_id))
    async def command(machine, action, directory, value, **kwargs):
        if action == 'read' and directory == failed_directory:
            return json.dumps(report or startup_report())
        return await original(machine, action, directory, value, **kwargs)
    manager.command = command
    await manager.advance(run_id)
    return command


async def test_retry_survives_restart_and_preserves_turn_model_author_queue_and_inputs(durable):
    manager, cloud, run_id = durable
    manager.store.execute("UPDATE messages SET user_id='google:alice' WHERE run_id=?", (run_id,))
    command = await fail_startup_once(manager, cloud, run_id)
    state = manager.state(run_id)
    original_spec = dict(cloud.machines[0].spec)
    original_author = manager.store.run(run_id)['active_user_id']
    assert state['phase'] == 'startup_wait' and state['segment'] == 0
    assert manager.store.run(run_id)['status'] == 'reconnecting'
    assert not manager.store.run(run_id)['pending_result']
    assert not [m for m in manager.store.messages(run_id) if m['role'] == 'assistant']
    manager.store.enqueue_message(run_id, 'Later', 'later', user_id='google:bob')
    successor = cloud.attach(TemporalRunManager(Store(manager.settings.data_dir), manager.settings))
    successor.command = command
    assert (await successor.advance(run_id))['retry_seconds'] > 0
    state['retry_at'] = 0
    successor.save(run_id, state)
    await drive(successor, run_id)
    assert len(cloud.machines) == 1 and len(cloud.launches) == 2
    assert cloud.launches[1] == cloud.launches[0] + '-startup-1'
    assert cloud.machines[0].spec == original_spec
    assert cloud.launch_tokens[0] == cloud.launch_tokens[1]
    assert manager.store.run(run_id)['active_user_id'] == original_author
    assert next(m for m in manager.store.messages(run_id) if m['content'] == 'Later')['status'] == 'queued'
    assert [m['content'] for m in manager.store.messages(run_id) if m['role'] == 'assistant'] == ['Saved answer']


@pytest.mark.parametrize('unsafe', ['model_started','execution_started','exit_code','marker','missing_baseline'])
async def test_unknown_or_executed_work_is_never_relaunched(durable, unsafe):
    manager, cloud, run_id = durable
    report = startup_report()
    await drive(manager, run_id, phase='monitor')
    if unsafe == 'model_started':
        manager.store.execute('UPDATE runs SET turn_model_calls=1 WHERE id=?', (run_id,))
    elif unsafe == 'execution_started':
        report['events'] = [{'kind':'status','message':'Started','data':{'phase':'execution_started'}}]
    elif unsafe == 'exit_code':
        report['exit_code'] = 1
    elif unsafe == 'marker':
        report['final']['startup_retry']['reason'] = 'HTTP 401'
    else:
        state = manager.state(run_id)
        state.pop('startup_model_calls')
        manager.save(run_id, state)
    await fail_startup_once(manager, cloud, run_id, report=report)
    await drive(manager, run_id)
    assert len(cloud.launches) == 1
    assert manager.store.run(run_id)['status'] == 'failed'
    assert 'safe startup retry could not be confirmed' in manager.store.run(run_id)['summary']


@pytest.mark.parametrize('stop', [True, False])
async def test_stopping_or_recovery_deadline_prevents_another_launch(durable, stop):
    manager, cloud, run_id = durable
    await fail_startup_once(manager, cloud, run_id)
    if stop:
        manager.store.update_run(run_id, status='stopping')
    else:
        state = manager.state(run_id)
        state['startup_deadline'] = 0
        manager.save(run_id, state)
    await drive(manager, run_id)
    assert len(cloud.launches) == 1 and len(cloud.terminations) == 1
    assert manager.store.run(run_id)['status'] == ('cancelled' if stop else 'failed')


async def test_retry_can_replace_lost_bootstrap_sandbox_from_previous_checkpoint(durable):
    manager, cloud, run_id = durable
    manager.store.update_run(run_id, snapshot_id='im-prior')
    await fail_startup_once(manager, cloud, run_id)
    cloud.machines[0].alive = False
    state = manager.state(run_id)
    state['retry_at'] = 0
    manager.save(run_id, state)
    await drive(manager, run_id)
    assert len(cloud.machines) == 2 and len(cloud.launches) == 2
    assert cloud.machines[1].spec['continuation'] is False
    assert manager.store.run(run_id)['status'] == 'idle'


async def test_startup_retry_after_checkpoint_keeps_continuation_and_prior_model_calls(durable):
    manager, cloud, run_id = durable
    cloud.continue_once = True
    await drive(manager, run_id, phase='checkpointed')
    manager.store.execute('UPDATE runs SET turn_model_calls=4 WHERE id=?', (run_id,))
    await fail_startup_once(manager, cloud, run_id)
    state = manager.state(run_id)
    assert state['phase'] == 'startup_wait' and state['segment'] == 1
    assert state['snapshot_id'] == 'im-1' and state['startup_model_calls'] == 4
    state['retry_at'] = 0
    manager.save(run_id, state)
    await drive(manager, run_id)
    assert cloud.machines[0].spec['continuation'] is True
    assert len(cloud.launches) == 3
    assert len([m for m in manager.store.messages(run_id) if m['role']=='assistant']) == 1
    assert manager.store.run(run_id)['turn_model_calls'] == 4
