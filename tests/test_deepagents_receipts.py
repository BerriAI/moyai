"""Supplementary receipt and originating-turn regressions, not deployment E2E."""
import asyncio
import json
import time

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from agent.activity import ActivityReporter
from app.failure_explanations import explain
from sandbox.broker_transport import unseal
from test_deepagents_runtime import durable_app, traced_tool_runtime
from test_trace_outbox import spans


DEFAULT_RESULT = object()


def broker(probe, receipt_status='completed', result=DEFAULT_RESULT, *, after_receipt=None, name='credentials_run'):
    calls = []
    result = {'saved': True} if result is DEFAULT_RESULT else result

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json=[{'name': name}])
        route = '/agent-runtime' if name == 'credentials_run' else '/tools/call'
        assert request.url.path.endswith(route)
        token = probe.runtime.runner.token(probe.scope.run_id, probe.scope.message_id)
        calls.append(json.loads(unseal(token, route, request.content)))
        if after_receipt:
            after_receipt()
        body = {'status': receipt_status, 'result': result} if route == '/agent-runtime' else result
        return httpx.Response(200, json=body)

    probe.runtime.broker_transport = httpx.MockTransport(respond)
    return calls


def captured(probe):
    rows = probe.store.rows('SELECT payload FROM trace_outbox')
    assert len(rows) == 1
    exported = spans(rows[0]['payload'])
    assert len(exported) == 1
    span = exported[0]
    return span, {attr.key: attr.value.string_value for attr in span.attributes}, rows[0]['payload']


@pytest.mark.parametrize('receipt_status,is_error', [
    ('completed', False), ('completed', True), ('unknown', True), ('cancelled', True), ('rejected', True),
])
async def test_terminal_receipt_enum_is_additive_to_result_and_canonical_diagnostics(
        traced_tool_runtime, receipt_status, is_error):
    probe = traced_tool_runtime
    result = {'isError': is_error, 'content': [{'type': 'text', 'text': 'private-result'}]}
    if receipt_status == 'unknown':
        result['diagnostic'] = {'exception_type': 'ConnectionError', 'failure_stage': 'computer_poll',
                                'request_id': 'untrusted-request'}
    calls = broker(probe, receipt_status, result)
    returned = await probe.runtime._call_tool(probe.scope, 'receipt', 'credentials_run', {'code': 'private-input'})
    assert returned == {'result': result}
    assert len(calls) == 1
    span, attrs, payload = captured(probe)
    assert attrs['moyai.tool.receipt_status'] == receipt_status
    assert attrs['moyai.status'] == ('error' if is_error else 'completed')
    assert span.status.code == (2 if is_error else 1)
    assert attrs['gen_ai.tool.call.id'] == calls[0]['request_id']
    if receipt_status == 'unknown':
        assert attrs['moyai.error.exception_type'] == 'ConnectionError'
        assert attrs['moyai.error.failure_stage'] == 'computer_poll'
        assert attrs['moyai.error.request_id'] == calls[0]['request_id']
    assert b'private-input' not in payload and b'private-result' not in payload
    assert not any(event['kind'] == 'trace' for event in probe.store.events(probe.scope.run_id))


@pytest.mark.parametrize('receipt_status', ['unknown', 'rejected', 'cancelled'])
@pytest.mark.parametrize('result', [
    'private-result', ['private-result'], None, {'saved': True},
    {'status': 'completed', 'outcome': 'completed', 'success': True, 'isError': False,
     'receipt_status': 'completed', 'retryable': True, 'dispatched': False},
    {'status': 'yielded_to_background'},
], ids=['string', 'list', 'null', 'benign-dict', 'misleading-dict', 'backgrounded-dict'])
@pytest.mark.parametrize('later_failure', [None, 'tool_receipt', 'tool_journal'])
async def test_trusted_non_success_receipt_survives_body_shape_and_later_failure(
        traced_tool_runtime, monkeypatch, receipt_status, result, later_failure):
    probe = traced_tool_runtime
    calls = broker(probe, receipt_status, result)
    original = json.dumps(result)

    def fail(*args, **kwargs):
        raise RuntimeError('private-publication-error')

    if later_failure == 'tool_receipt':
        monkeypatch.setattr(ActivityReporter, 'complete', fail)
    elif later_failure == 'tool_journal':
        monkeypatch.setattr(probe.runtime, 'append', fail)
    if later_failure:
        with pytest.raises(RuntimeError, match='private-publication-error'):
            await probe.runtime._call_tool(probe.scope, 'receipt-shape', 'credentials_run', {})
    else:
        assert await probe.runtime._call_tool(probe.scope, 'receipt-shape', 'credentials_run', {}) == {'result': result}
    assert json.dumps(result) == original
    assert len(calls) == 1
    span, attrs, payload = captured(probe)
    assert attrs['moyai.tool.receipt_status'] == receipt_status
    assert attrs['moyai.status'] == 'error' and span.status.code == 2
    events = probe.store.events(probe.scope.run_id)
    activities = [event['data'] for event in events if event['kind'] == 'tool']
    assert [data['phase'] for data in activities] == (
        ['started'] if later_failure == 'tool_receipt' else ['started', 'error'])
    records = [data for data in activities if data['phase'] == 'error']
    if later_failure:
        failure = next(event['data'] for event in events if event['data'].get('phase') == 'tool_failure')
        assert failure['failure_stage'] == later_failure
        records.append(failure)
    for data in records:
        assert data['turn_id'] == probe.scope.message_id
        assert data['action']['outcome'] == receipt_status
        explanation = explain(data)
        assert explanation['action']['outcome'] == receipt_status
        assert explanation['retryability'] == (
            'blocked_unknown_outcome' if receipt_status == 'unknown' else 'not_retryable')
    assert b'private-result' not in payload and b'private-publication-error' not in payload


@pytest.mark.parametrize('result,expected_phase', [
    ('ordinary result', 'completed'), (['ordinary result'], 'completed'), (None, 'completed'),
    ({'isError': False, 'receipt_status': 'unknown'}, 'completed'),
    ({'isError': True, 'receipt_status': 'completed'}, 'error'),
    ({'exit_code': 1}, 'error'),
])
async def test_completed_receipt_preserves_body_level_success_or_error(
        traced_tool_runtime, result, expected_phase):
    probe = traced_tool_runtime
    calls = broker(probe, 'completed', result)
    assert await probe.runtime._call_tool(probe.scope, 'completed-shape', 'credentials_run', {}) == {'result': result}
    assert len(calls) == 1
    span, attrs, _ = captured(probe)
    assert attrs['moyai.tool.receipt_status'] == 'completed'
    assert attrs['moyai.status'] == expected_phase
    assert span.status.code == (2 if expected_phase == 'error' else 1)
    activity = [e['data'] for e in probe.store.events(probe.scope.run_id) if e['kind'] == 'tool'][-1]
    assert activity['phase'] == expected_phase
    if isinstance(result, dict) and 'exit_code' in result:
        assert activity['exit_code'] == result['exit_code']


@pytest.mark.parametrize('untrusted', ['queued', 'private-status', ['completed'], {'status': 'unknown'}, 200, True, None])
async def test_receipt_attribute_accepts_only_terminal_enums(traced_tool_runtime, untrusted):
    probe = traced_tool_runtime
    stamp = time.time_ns()
    probe.tracing.tool(probe.scope.run_id, {'tool': 'credentials_run', 'call_id': 'receipt',
        'start_ns': stamp, 'end_ns': stamp, 'receipt_status': untrusted}, message_id=probe.scope.message_id)
    _, attrs, payload = captured(probe)
    assert 'moyai.tool.receipt_status' not in attrs
    assert b'private-status' not in payload


@pytest.mark.parametrize('spoof', ['unknown', 'rejected', 'cancelled', 'completed'])
async def test_noncomputer_result_cannot_supply_computer_receipt_metadata(traced_tool_runtime, spoof):
    probe = traced_tool_runtime
    result = {'saved': True, 'receipt_status': spoof}
    calls = broker(probe, result=result, name='notion_append')
    assert await probe.runtime._call_tool(probe.scope, 'connector', 'notion_append', {}) == {'result': result}
    assert len(calls) == 1
    span, attrs, _ = captured(probe)
    assert 'moyai.tool.receipt_status' not in attrs
    assert attrs['moyai.status'] == 'completed' and span.status.code == 1
    activity = [e['data'] for e in probe.store.events(probe.scope.run_id) if e['kind'] == 'tool'][-1]
    assert activity['phase'] == 'completed'


@pytest.mark.parametrize('omit_original', [False, True])
async def test_late_receipt_activity_and_privacy_use_originating_turn(traced_tool_runtime, omit_original):
    probe = traced_tool_runtime
    other = probe.store.identity({'method': 'google', 'identity': {'sub': 'next-user', 'email': 'next@example.com'}})
    probe.tracing.preferences.save(probe.scope.actor_id, {'omit_private_tool_payloads': omit_original})
    probe.tracing.preferences.save(other, {'omit_private_tool_payloads': not omit_original})
    next_message, _ = probe.store.enqueue_message(probe.scope.run_id, 'Next turn', 'next', user_id=other)

    def advance():
        probe.store.execute('UPDATE runs SET active_message_id=?,active_user_id=? WHERE id=?',
                            (next_message['id'], other, probe.scope.run_id))

    calls = broker(probe, result={'content': 'private-result'}, after_receipt=advance)
    with pytest.raises(ApplicationError) as caught:
        await probe.runtime._call_tool(probe.scope, 'late', 'credentials_run', {'code': 'private-input'})
    assert caught.value.type == 'CapabilityExpired'
    assert len(calls) == 1
    span, attrs, payload = captured(probe)
    assert span.status.code == 1 and attrs['moyai.status'] == 'completed'
    assert attrs['moyai.tool.receipt_status'] == 'completed'
    assert attrs['moyai.turn_id'] == str(probe.scope.message_id)
    assert attrs['user.id'] == 'trace@example.com'
    expected = '[private tool payload omitted]' if omit_original else 'null'
    assert attrs['input.value'] == attrs['output.value'] == expected
    events = probe.store.events(probe.scope.run_id)
    activities = [event['data'] for event in events if event['kind'] == 'tool']
    assert [data['phase'] for data in activities] == ['started', 'completed']
    assert all(data['turn_id'] == probe.scope.message_id for data in activities)
    diagnostic = next(event['data'] for event in events if event['data'].get('phase') == 'tool_failure')
    assert diagnostic['turn_id'] == probe.scope.message_id
    assert diagnostic['failure_stage'] == 'tool_journal'
    assert b'private-input' not in payload and b'private-result' not in payload
    assert 'private-input' not in json.dumps(events) and 'private-result' not in json.dumps(events)
    current = probe.store.run(probe.scope.run_id)
    assert current['active_message_id'] == next_message['id'] and current['active_user_id'] == other


@pytest.mark.parametrize('failure_type', [RuntimeError, asyncio.CancelledError])
async def test_terminal_metadata_survives_publication_failure_without_reclassification(
        traced_tool_runtime, monkeypatch, failure_type):
    probe = traced_tool_runtime
    calls = broker(probe)

    def fail(*args, **kwargs):
        raise failure_type('private-publication-error')

    monkeypatch.setattr(probe.runtime, 'append', fail)
    with pytest.raises(failure_type):
        await probe.runtime._call_tool(probe.scope, 'publication', 'credentials_run', {})
    assert len(calls) == 1
    span, attrs, payload = captured(probe)
    assert span.status.code == 1
    assert attrs['moyai.status'] == attrs['moyai.tool.receipt_status'] == 'completed'
    diagnostic = next(event['data'] for event in probe.store.events(probe.scope.run_id)
                      if event['data'].get('phase') == 'tool_failure')
    assert diagnostic['failure_stage'] == 'tool_journal'
    assert diagnostic['exception_type'] == failure_type.__name__
    assert b'private-publication-error' not in payload


@pytest.mark.parametrize('traced_tool_runtime', [True], indirect=True)
async def test_private_run_does_not_export_receipt_metadata(traced_tool_runtime):
    probe = traced_tool_runtime
    calls = broker(probe)
    assert await probe.runtime._call_tool(probe.scope, 'private', 'credentials_run', {}) == {'result': {'saved': True}}
    assert len(calls) == 1
    assert not probe.store.rows('SELECT payload FROM trace_outbox')
    assert not probe.store.rows('SELECT * FROM trace_contexts WHERE run_id=?', (probe.scope.run_id,))
