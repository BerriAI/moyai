"""Faults cross the actual runtime, Temporal converter, storage and OTLP wire."""
import asyncio
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.client import WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from app.deepagents_diagnostics import workflow_failure as terminal_failure
from app.workspace_diagnostics import failure_record
from sandbox.broker_failure import exception_diagnostic, safe_error
from sandbox.durable_process import status, supervise
from test_deepagents_runtime import durable_app, traced_tool_runtime  # noqa: F401
from test_deepagents_failures import fail_workflow, workflow_failure
from test_trace_outbox import spans, transport


PRIVATE = 'sk-private-sentinel xoxb-private-token Authorization=Bearer super-secret command=curl-private'


def attributes(span):
    return {item.key: item.value.string_value for item in span.attributes}


@pytest.mark.parametrize('stage', ['computer_poll', 'computer_step', 'tool_dispatch'])
async def test_original_failure_survives_temporal_and_exports_tool_root_hierarchy(traced_tool_runtime, monkeypatch, stage):
    probe = traced_tool_runtime
    dispatched, captured = [], []

    async def explode(*args, **kwargs):
        try:
            raise OSError(13, PRIVATE, '/private/credential-directory')
        except OSError as cause:
            raise httpx.ReadTimeout(PRIVATE) from cause

    async def broker(request):
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=[{'name': 'github_checkout'}])
        dispatched.append(request)
        if stage == 'computer_step':
            return httpx.Response(200, json={'status': 'queued'})
        return await explode()

    if stage == 'tool_dispatch':
        monkeypatch.setattr('app.deepagents_runtime.requires_computer', lambda name: False)
    monkeypatch.setattr(probe.runtime.runner.agent_runtime, 'computer_step', explode)
    probe.runtime.broker_transport = httpx.MockTransport(broker)
    await transport(probe.tracing, lambda request: captured.append(request.content) or httpx.Response(200))
    with pytest.raises(ApplicationError) as caught:
        await probe.invoke()
    assert caught.value.type == 'UncertainToolOutcome' and caught.value.non_retryable
    assert len(dispatched) == 1
    detail = caught.value.details[0]
    assert detail['failure_stage'] == stage
    assert detail['exception_chain'] == ['ReadTimeout', 'PermissionError']
    assert detail['errno'] == 13
    assert any(location.startswith('app/deepagents_runtime.py:') for location in detail['stack_locations'])
    assert all(location.startswith(('app/', 'agent/', 'sandbox/')) for location in detail['stack_locations'])
    assert str(Path.cwd()) not in str(detail)

    encoded = Failure()
    await DataConverter.default.encode_failure(workflow_failure(caught.value).cause, encoded)
    decoded = WorkflowFailureError(cause=await DataConverter.default.decode_failure(encoded))
    assert decoded.cause.cause.non_retryable
    events = await fail_workflow(probe.runtime.runner, probe.store, probe.scope, decoded)
    assert events[0]['data']['exception_type'] == 'ReadTimeout'
    assert events[0]['data']['outcome_type'] == 'UncertainToolOutcome'
    assert events[0]['data']['request_id'] == detail['request_id']
    public = failure_record({**events[0], 'data': json.dumps(events[0]['data'])})
    assert public['stack_locations'] == detail['stack_locations']
    probe.tracing.finish_turn(probe.scope.run_id, probe.scope.message_id, 'Execution failed.', 'failed')
    assert await probe.tracing.outboxes[0].export_once()
    exported = [span for wire in captured for span in spans(wire)]
    tool = next(span for span in exported if attributes(span).get('openinference.span.kind') == 'TOOL')
    root = next(span for span in exported if attributes(span).get('openinference.span.kind') == 'AGENT')
    assert tool.trace_id == root.trace_id and tool.parent_span_id == root.span_id
    for span in (tool, root):
        values = attributes(span)
        assert span.status.code == 2
        assert values['error.type'] == 'ReadTimeout'
        assert values['moyai.error.failure_stage'] == stage
        assert values['moyai.error.request_id'] == detail['request_id']
        assert 'ReadTimeout' in span.status.message and 'Model request' not in span.status.message
        assert attributes(span.events[0])['exception.type'] == 'ReadTimeout'
    evidence = b''.join(captured) + str(encoded).encode() + str(probe.store.events(probe.scope.run_id)).encode()
    for secret in PRIVATE.split() + ['/private/credential-directory', 'private-argument-sentinel']:
        # Tool activity already has its separately sanitized public input;
        # diagnostics, Temporal failure and OTLP never include that input.
        assert secret.encode() not in b''.join(captured) + str(encoded).encode()
        if secret != 'private-argument-sentinel':
            assert secret.encode() not in evidence


@pytest.mark.parametrize('telemetry_failure', [RuntimeError, asyncio.CancelledError])
async def test_diagnostic_storage_and_export_failures_do_not_replace_original_failure(
        traced_tool_runtime, monkeypatch, telemetry_failure):
    probe = traced_tool_runtime
    original_event = probe.store.event

    def event(run_id, kind, message, data=None, **kwargs):
        if isinstance(data, dict) and data.get('phase') == 'tool_failure':
            raise telemetry_failure(PRIVATE)
        return original_event(run_id, kind, message, data, **kwargs)

    async def broker(request):
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=[{'name': 'github_checkout'}])
        raise ValueError(PRIVATE)

    def failed_trace(*args, **kwargs):
        raise telemetry_failure(PRIVATE)

    monkeypatch.setattr(probe.store, 'event', event)
    monkeypatch.setattr(probe.tracing, 'tool', failed_trace)
    probe.runtime.broker_transport = httpx.MockTransport(broker)
    with pytest.raises(ApplicationError) as caught:
        await probe.invoke()
    assert caught.value.non_retryable and caught.value.type == 'UncertainToolOutcome'
    assert caught.value.details[0]['exception_type'] == 'ValueError'


@pytest.mark.parametrize('cancelled', [False, True])
async def test_failed_diagnostic_collection_preserves_original_outcome(traced_tool_runtime, monkeypatch, cancelled):
    probe = traced_tool_runtime
    original = asyncio.CancelledError(PRIVATE) if cancelled else ValueError(PRIVATE)

    async def broker(request):
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=[{'name': 'github_checkout'}])
        raise original

    def failed_capture(*args, **kwargs):
        raise RuntimeError('Diagnostic collector unavailable')

    monkeypatch.setattr('app.deepagents_runtime.exception_diagnostic', failed_capture)
    probe.runtime.broker_transport = httpx.MockTransport(broker)
    with pytest.raises(asyncio.CancelledError if cancelled else ApplicationError) as caught:
        await probe.invoke()
    if cancelled:
        assert caught.value is original
    else:
        assert caught.value.type == 'UncertainToolOutcome' and caught.value.non_retryable
    assert 'Diagnostic collector unavailable' not in str(caught.value)


async def test_computer_poll_broker_status_keeps_semantic_type_in_uncertain_failure(traced_tool_runtime):
    probe = traced_tool_runtime
    probe.runtime.broker_transport = httpx.MockTransport(lambda request: httpx.Response(
        200 if request.url.path.endswith('/tools') else 500,
        json=[{'name': 'github_checkout'}] if request.url.path.endswith('/tools') else {'error': PRIVATE}))
    with pytest.raises(ApplicationError) as caught:
        await probe.invoke()
    assert caught.value.type == 'UncertainToolOutcome' and caught.value.non_retryable
    detail = caught.value.details[0]
    assert detail['exception_type'] == 'BrokerRejected'
    assert detail['exception_chain'] == ['BrokerRejected']
    assert detail['http_status'] == 500
    assert detail['failure_stage'] == 'computer_poll'
    root = terminal_failure(workflow_failure(caught.value))
    assert root['exception_type'] == 'BrokerRejected' and root['http_status'] == 500
    span = spans(probe.store.rows('SELECT payload FROM trace_outbox')[0]['payload'])[0]
    assert attributes(span)['error.type'] == 'BrokerRejected'
    assert PRIVATE not in str(detail)


@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
async def test_confirmed_receipt_failure_is_identifiable_but_never_uncertain(traced_tool_runtime, monkeypatch, error_type):
    probe = traced_tool_runtime
    failure = error_type(PRIVATE)

    async def broker(request):
        return httpx.Response(200, json=([{'name': 'github_checkout'}] if request.url.path.endswith('/tools')
            else {'status': 'completed', 'result': {'isError': False, 'saved': True}}))

    def fail_append(*args, **kwargs):
        raise failure

    probe.runtime.broker_transport = httpx.MockTransport(broker)
    monkeypatch.setattr(probe.runtime, 'append', fail_append)
    with pytest.raises(error_type) as caught:
        await probe.invoke()
    # The cancellation-draining database helper may wrap a worker cancellation,
    # but must retain that original cause and its confirmed-action semantics.
    assert caught.value is failure or (
        error_type is asyncio.CancelledError and caught.value.__cause__ is failure)
    records = probe.store.events(probe.scope.run_id)
    diagnostic = next(event['data'] for event in records if event['data'].get('phase') == 'tool_failure')
    assert diagnostic['failure_stage'] == 'tool_journal'
    assert diagnostic['exception_type'] == error_type.__name__
    span = spans(probe.store.rows('SELECT payload FROM trace_outbox')[0]['payload'])[0]
    assert span.status.code == 1  # The external action was already confirmed.
    assert 'error.type' not in attributes(span)
    assert PRIVATE not in str(records)


@pytest.mark.parametrize('omit_private', [False, True])
async def test_private_bridge_exception_keeps_error_receipt_and_exports_only_diagnostic(
        traced_tool_runtime, monkeypatch, omit_private):
    from agent.tools import mcp_bridge
    from sandbox.broker_transport import unseal

    probe = traced_tool_runtime
    probe.tracing.preferences.save(probe.scope.actor_id, {'omit_private_tool_payloads': omit_private})
    calls = []
    monkeypatch.delenv('MOYAI_REMOTE_COMPUTER', raising=False)
    monkeypatch.setattr(mcp_bridge, 'broker', lambda *args: [{'name': 'credentials_run'}])

    def execute(args, broker):
        calls.append(args)
        try:
            raise TimeoutError(PRIVATE)
        except TimeoutError as cause:
            raise ValueError(PRIVATE) from cause

    monkeypatch.setattr(mcp_bridge.credential_tools, 'run', execute)

    def broker(request):
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=[{'name': 'credentials_run'}])
        token = probe.runtime.runner.token(probe.scope.run_id, probe.scope.message_id)
        result = mcp_bridge.call_tool(json.loads(unseal(token, '/agent-runtime', request.content)), [])
        assert result['content'] == [{'type': 'text', 'text':
            'Tool failed (ValueError). The action was not confirmed; do not retry writes automatically.'}]
        return httpx.Response(200, json={'status': 'completed', 'result': result})

    probe.runtime.broker_transport = httpx.MockTransport(broker)
    result = await probe.runtime._call_tool(probe.scope, 'private-call', 'credentials_run', {'command': PRIVATE})
    assert len(calls) == 1 and result['result']['isError'] is True
    assert result['result']['error_type'] == 'ValueError'
    assert result['result']['diagnostic']['exception_chain'] == ['ValueError', 'TimeoutError']
    assert any(item.startswith('agent/tools/mcp_bridge.py:') for item in result['result']['diagnostic']['stack_locations'])
    wire = probe.store.rows('SELECT payload FROM trace_outbox')[0]['payload']
    span = spans(wire)[0]
    attrs = attributes(span)
    assert attrs['moyai.error.failure_stage'] == 'tool_dispatch'
    assert attrs['error.type'] == 'ValueError'
    assert attrs['input.value'] == attrs['output.value'] == ('[private tool payload omitted]' if omit_private else 'null')
    event = next(event for event in probe.store.events(probe.scope.run_id)
                 if event['data'].get('phase') == 'tool_failure')
    assert event['data']['request_id'] == attrs['gen_ai.tool.call.id']
    assert PRIVATE.encode() not in wire and PRIVATE not in str(event)


def test_diagnostics_are_bounded_and_ignore_secret_messages_and_foreign_frames():
    try:
        raise ValueError(PRIVATE)
    except ValueError as cause:
        try:
            raise RuntimeError(PRIVATE) from cause
        except RuntimeError as error:
            diagnostic = exception_diagnostic(error, stage='tool_dispatch', request_id='a' * 32)
    assert diagnostic == {'failure_stage': 'tool_dispatch', 'request_id': 'a' * 32,
                         'exception_type': 'RuntimeError', 'cause_type': 'ValueError',
                         'exception_chain': ['RuntimeError', 'ValueError']}
    malicious = {**diagnostic, 'message': PRIVATE, 'stderr': PRIVATE, 'locals': {'token': PRIVATE},
                 'stack_locations': ['/Users/private-secret.py:1:run', 'app/main.py:12:run\n' + PRIVATE],
                 'exception_chain': ['RuntimeError', PRIVATE] * 20}
    sanitized = safe_error(malicious)
    assert PRIVATE not in str(sanitized)
    assert len(sanitized['exception_chain']) <= 6
    assert 'stack_locations' not in sanitized
    wrapped = ApplicationError(PRIVATE, malicious, type='UncertainToolOutcome', non_retryable=True)
    assert PRIVATE not in str(terminal_failure(workflow_failure(wrapped)))


def test_hostile_exception_properties_do_not_break_diagnostics():
    class BrokenError(Exception):
        @property
        def errno(self):
            raise RuntimeError(PRIVATE)
    assert exception_diagnostic(BrokenError(PRIVATE), stage='tool_dispatch')['exception_type'] == 'BrokenError'


@pytest.mark.parametrize('boundary', ['bridge', 'remote', 'supervisor'])
def test_sandbox_collector_failure_preserves_error_receipt_or_terminal_exit(boundary, tmp_path, monkeypatch):
    from agent.tools import mcp_bridge
    from sandbox import remote_tool, durable_process
    def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    if boundary == 'bridge':
        monkeypatch.setattr(mcp_bridge, 'exception_diagnostic', unavailable)
        result = mcp_bridge.call_tool({'name': 'browser_unknown'}, [])
        assert result['isError'] and result['error_type'] == 'ValueError'
        assert PRIVATE not in json.dumps(result)
    elif boundary == 'remote':
        monkeypatch.setattr(remote_tool, 'exception_diagnostic', unavailable)
        monkeypatch.delenv('WORKSPACE_RUN_TOKEN', raising=False)
        assert remote_tool.run({'workspace': str(tmp_path), 'broker_url': 'http://unused.invalid'}) == 1
    else:
        monkeypatch.setattr(durable_process, 'exception_diagnostic', unavailable)
        directory = tmp_path / 'operation'
        supervise(directory, ['/missing/' + PRIVATE])
        report = status(directory)
        assert report['state'] == 'done' and report['exit_code'] == 1
        assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize('launch_failure', [False, True])
def test_supervisor_failure_has_safe_receipt_without_stderr_or_command(tmp_path, launch_failure):
    directory = tmp_path / 'operation'
    command = ['/missing/' + PRIVATE] if launch_failure else [
        sys.executable, '-c', 'import sys; sys.stderr.write('+repr(PRIVATE)+'); sys.exit(7)']
    supervise(directory, command)
    report = status(directory)
    assert report['state'] == 'done'
    assert report['diagnostic']['exception_type'] == ('FileNotFoundError' if launch_failure else 'ProcessExit')
    assert report['diagnostic']['failure_stage'] == ('supervisor_launch' if launch_failure else 'supervisor_wait')
    assert report['diagnostic']['exit_code'] == (1 if launch_failure else 7)
    assert PRIVATE not in json.dumps(report)
    if launch_failure:
        assert report['diagnostic']['errno'] == 2
        assert any('sandbox/durable_process.py:' in item for item in report['diagnostic']['stack_locations'])
    before = report.copy()
    supervise(directory, [sys.executable, '-c', "raise AssertionError('must not replay')"])
    assert status(directory) == before


@pytest.mark.parametrize('null_scope', [False, True])
@pytest.mark.parametrize('confirmed', [False, True])
async def test_supervisor_receipt_is_persisted_before_guest_cleanup(
        traced_tool_runtime, monkeypatch, tmp_path, null_scope, confirmed):
    probe = traced_tool_runtime
    directory = tmp_path / 'operation'
    diagnostic = {'exception_type': 'OSError', 'failure_stage': 'remote_activate',
                  'exception_chain': ['OSError'], 'request_id': 'untrusted-sandbox-id'}
    event = {'kind': 'error', 'message': 'Computer operation failed.', 'data': diagnostic}
    command = [sys.executable, '-c',
        "import json,sys\nfor i in range(35): print('WORKSPACE_EVENT '+json.dumps({'kind':'status'}))\n"
        "print('WORKSPACE_EVENT '+" + repr(json.dumps(event)) + ")\nsys.stderr.write(" + repr(PRIVATE) + ")\nsys.exit(1)"]
    supervise(directory, command)
    report = status(directory)
    assert len(report['events']) == 30 and report['diagnostic']['exception_type'] == 'OSError'
    if confirmed:
        report['final'] = {'tool_result': {'saved': True}}
    manager, scope = probe.runtime.runner, probe.scope
    state = manager.state(scope.run_id)
    monkeypatch.setattr(manager.agent_runtime.operations, 'input_id', lambda *args: scope.message_id)
    monkeypatch.setattr(manager, 'spec', lambda run: {'broker_tool_scope': None})
    sandbox = SimpleNamespace(filesystem=SimpleNamespace(write_text=SimpleNamespace(aio=AsyncMock())))
    async def command(*args, **kwargs):
        return json.dumps(report) if args[1] == 'read' else ''
    monkeypatch.setattr(manager, 'command', command)
    outcome = await manager.agent_runtime.job(scope.run_id, state, sandbox, 'finish' if null_scope else 'a' * 32,
                                             {'name': 'credentials_run', 'arguments': {'command': PRIVATE}})
    assert outcome['status'] == ('completed' if confirmed else 'unknown')
    if confirmed:
        assert outcome['result'] == {'saved': True}
    elif not null_scope:
        assert outcome['result']['diagnostic']['request_id'] == 'a' * 32
    shutil.rmtree(directory)  # Guest has gone; durable event and receipt remain.
    saved = next(event for event in probe.store.events(scope.run_id)
                 if event['data'].get('phase') == 'supervisor_failure')
    assert saved['data']['failure_stage'] == 'remote_activate'
    if null_scope:
        assert 'request_id' not in saved['data']
    else:
        assert saved['data']['request_id'] == 'a' * 32
    assert saved['data']['turn_id'] == scope.message_id
    assert PRIVATE not in str(saved) and 'untrusted-sandbox-id' not in str(saved)


def test_remote_setup_exception_has_stage_and_no_traceback(tmp_path, monkeypatch, capsys):
    from sandbox import remote_tool
    monkeypatch.delenv('WORKSPACE_RUN_TOKEN', raising=False)
    assert remote_tool.run({'workspace': str(tmp_path), 'broker_url': 'http://private.invalid'}) == 1
    captured = capsys.readouterr()
    event = json.loads(captured.out.removeprefix('WORKSPACE_EVENT '))
    assert event['data']['exception_type'] == 'KeyError'
    assert event['data']['failure_stage'] == 'remote_setup'
    assert any('sandbox/remote_tool.py:' in frame for frame in event['data']['stack_locations'])
    assert not captured.err
    assert 'WORKSPACE_RUN_TOKEN' not in captured.out and 'private.invalid' not in captured.out


def test_real_remote_process_failure_survives_supervisor_boundary(tmp_path, monkeypatch):
    from sandbox import remote_tool
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', PRIVATE)
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'workspace': '/missing/' + PRIVATE, 'broker_url': 'http://private.invalid',
        'name': 'credentials_run', 'arguments': {'command': PRIVATE},
        'broker_tool_scope': {'input_id': 1, 'request_id': 'b' * 32}}))
    directory = tmp_path / 'operation'
    supervise(directory, [sys.executable, str(Path(remote_tool.__file__).resolve()), str(spec)])
    report = status(directory)
    assert report['exit_code'] == 1 and 'final' not in report
    assert report['diagnostic']['exception_type'] == 'FileNotFoundError'
    assert report['diagnostic']['failure_stage'] == 'remote_activate'
    assert report['diagnostic']['request_id'] == 'b' * 32
    assert report['diagnostic']['errno'] == 2
    assert not (directory / 'stderr.log').read_bytes()
    assert PRIVATE not in json.dumps(report)


async def test_supervisor_control_failure_keeps_exit_code_without_stream_contents(traced_tool_runtime):
    from app.durable_runner import SupervisorCommandError
    stream = SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=PRIVATE)))
    process = SimpleNamespace(stdout=stream, stderr=stream, wait=SimpleNamespace(aio=AsyncMock(return_value=23)))
    sandbox = SimpleNamespace(exec=SimpleNamespace(aio=AsyncMock(return_value=process)))
    with pytest.raises(SupervisorCommandError) as caught:
        await traced_tool_runtime.runtime.runner.command(sandbox, 'read', '/private/operation', '0')
    diagnostic = exception_diagnostic(caught.value, stage='computer_step')
    assert diagnostic['exit_code'] == 23
    assert diagnostic['exception_type'] == 'SupervisorCommandError'
    assert any(item.startswith('app/durable_runner.py:') for item in diagnostic['stack_locations'])
    assert PRIVATE not in str(caught.value) + str(diagnostic)
