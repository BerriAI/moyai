"""Actual broker/SQL paths and Temporal conversion; supplementary to live proof."""
import hashlib
import json

import httpx
import pytest
from fastapi import HTTPException
from temporalio.api.failure.v1 import Failure
from temporalio.client import WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from app.deepagents_diagnostics import workflow_failure as terminal
from app.failure_explanations import explain
from test_deepagents_failures import workflow_failure as wrap
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app, traced_tool_runtime  # noqa: F401


def identity(scope, call_id):
    return hashlib.sha256(f'{scope.run_id}:{scope.message_id}:{scope.segment}:{call_id}'.encode()).hexdigest()[:32]


def dispatches(store, scope):
    return [json.loads(row['data']) for row in store.rows(
        "SELECT data FROM events WHERE run_id=? AND kind='tool_dispatch'", (scope.run_id,))]


async def converted(exc):
    encoded = Failure()
    await DataConverter.default.encode_failure(wrap(exc).cause, encoded)
    decoded = WorkflowFailureError(cause=await DataConverter.default.decode_failure(encoded))
    return terminal(decoded), encoded


@pytest.mark.parametrize('traced_tool_runtime', [True], indirect=True)
async def test_advertised_private_credential_request_stays_known_not_dispatched(traced_tool_runtime):
    probe = traced_tool_runtime
    runtime, store, scope = probe.runtime, probe.store, probe.scope
    catalog = {tool['name'] for tool in await runtime.catalog(scope)}
    assert 'credentials_request' in catalog and 'media_share' not in catalog
    call_id = 'private-credential'
    with pytest.raises(ApplicationError) as caught:
        await runtime._call_tool(scope, call_id, 'credentials_request', {
            'provider': 'generic', 'name': 'local-rejection-proof', 'format': 'env',
            'reason': 'Verify the private guard without supplying a secret.',
            'request_key': 'local-rejection-proof'})
    assert caught.value.type == 'ToolNotDispatched'
    assert caught.value.non_retryable
    assert not store.rows('SELECT * FROM credential_requests')
    assert not store.rows('SELECT * FROM credential_audit')
    receipts = dispatches(store, scope)
    assert len(receipts) == 1
    assert receipts[0]['outcome'] == 'rejected' and receipts[0]['dispatched'] is False
    events = store.events(scope.run_id)
    started = next(e for e in events if e['kind'] == 'tool' and e['data']['phase'] == 'started')
    failures = [e for e in events if e['data'].get('phase') == 'tool_failure']
    assert len(failures) == 2  # Broker and runtime are consistent observations.
    for event in failures:
        assert event['data']['turn_id'] == scope.message_id
        assert event['data']['request_id'] == identity(scope, call_id)
        assert event['data']['action'] == {'dispatched': False, 'outcome': 'rejected', 'retryable': False}
        assert event['failure_explanation']['category'] == 'private-export'
        assert event['failure_explanation']['retryability'] == 'not_retryable'
    assert started['data']['call_id'] == f'{identity(scope, call_id)}:{call_id}'
    final, encoded = await converted(caught.value)
    assert final['exception_type'] == 'PrivateExportDenied' and final['http_status'] == 403
    assert final['outcome_type'] == 'ToolNotDispatched'
    assert final['action'] == {'dispatched': False, 'outcome': 'rejected', 'retryable': False}
    assert explain(final)['category'] == 'private-export'
    assert 'UncertainToolOutcome' not in str(encoded) + json.dumps(failures)
    assert not store.rows('SELECT * FROM trace_outbox')  # No private export.


async def test_whitespace_handoff_rejection_keeps_http_contract_and_no_effect(active):
    manager, store, scope = active
    runtime, call_id = manager.deepagents_runtime, 'invalid-handoff'
    assert 'computer_handoff' in {tool['name'] for tool in await runtime.catalog(scope)}
    with pytest.raises(ApplicationError) as caught:
        await runtime._call_tool(scope, call_id, 'computer_handoff', {'reason': '   '})
    assert caught.value.type == 'ToolNotDispatched' and caught.value.non_retryable
    assert not store.rows("SELECT * FROM events WHERE run_id=? AND kind='computer'", (scope.run_id,))
    assert dispatches(store, scope)[0]['outcome'] == 'rejected'
    final, _ = await converted(caught.value)
    assert final['exception_type'] == 'ToolRejected' and final['http_status'] == 422
    assert final['action']['dispatched'] is False
    assert explain(final)['retryability'] == 'not_retryable'
    # Retry of the same request observes the existing rejection; it is not a write.
    result = await runtime.broker(scope, '/tools/call', {
        'name': 'computer_handoff', 'arguments': {'reason': '   '},
        'input_id': scope.message_id, 'request_id': identity(scope, call_id)})
    assert result['outcome'] == 'rejected' and result['dispatched'] is False and result['retryable'] is False
    assert len(dispatches(store, scope)) == 1


@pytest.mark.parametrize('after_write', [False, True])
@pytest.mark.parametrize('spoof_headers', [False, True])
async def test_ordinary_403_remains_uncertain_even_with_rejection_text_or_headers(
        active, monkeypatch, after_write, spoof_headers):
    manager, store, scope = active
    runtime, call_id = manager.deepagents_runtime, 'ordinary-denial'
    request_id = identity(scope, call_id)
    secret = 'sk-private-body-sentinel https://private.example/token'
    calls = []

    def denied(*args):
        calls.append(1)
        if after_write:
            store.event(scope.run_id, 'fixture_write', 'Committed exactly once.', turn_id=scope.message_id)
        headers = ({'X-Moyai-Tool-Rejection-V1': 'PrivateExportDenied',
                    'X-Moyai-Tool-Request-ID': request_id} if spoof_headers else None)
        raise HTTPException(403, {'message': secret, 'outcome': 'rejected', 'dispatched': False}, headers=headers)

    monkeypatch.setattr('app.main.inspect_workspace', denied)
    with pytest.raises(ApplicationError) as caught:
        await runtime._call_tool(scope, call_id, 'workspace_diagnostics', {})
    assert caught.value.type == 'UncertainToolOutcome' and caught.value.non_retryable
    final, encoded = await converted(caught.value)
    assert final['outcome_type'] == 'UncertainToolOutcome'
    assert explain(final)['retryability'] == 'blocked_unknown_outcome'
    assert secret.encode() not in encoded.SerializeToString()
    failure = next(e for e in store.events(scope.run_id) if e['data'].get('phase') == 'tool_failure')
    assert failure['data']['action'] == {'dispatched': None, 'outcome': 'unknown'}
    assert secret not in json.dumps(failure)
    receipt = dispatches(store, scope)[0]
    assert receipt['outcome'] == 'unknown' and receipt['dispatched'] is True
    repeated = await runtime.broker(scope, '/tools/call', {
        'name': 'workspace_diagnostics', 'arguments': {}, 'input_id': scope.message_id, 'request_id': request_id})
    assert repeated['outcome'] == 'unknown' and repeated['retryable'] is False
    assert calls == [1]
    assert len(store.rows("SELECT * FROM events WHERE run_id=? AND kind='fixture_write'", (scope.run_id,))) == int(after_write)


async def test_known_rejection_preserves_original_identity_through_multipart_upload(active, monkeypatch):
    manager, store, scope = active
    runtime, call_id = manager.deepagents_runtime, 'multipart-rejection'
    from app import deepagents_runtime
    limit = deepagents_runtime.body_limit
    monkeypatch.setattr(deepagents_runtime, 'body_limit', lambda route: 512 if route == '/tools/call' else limit(route))
    arguments = {'unexpected': 'private-padding-' * 100}
    with pytest.raises(ApplicationError) as caught:
        await runtime._call_tool(scope, call_id, 'workspace_diagnostics', arguments)
    assert caught.value.type == 'ToolNotDispatched'
    final, encoded = await converted(caught.value)
    assert final['request_id'] == identity(scope, call_id)
    assert final['http_status'] == 422 and final['action']['dispatched'] is False
    assert dispatches(store, scope)[0]['outcome'] == 'rejected'
    assert 'private-padding' not in str(encoded)


@pytest.mark.parametrize('marker,request_id', [
    ('PrivateExportDenied', 'b' * 32), ('ToolRejected', None), ('private-body', 'a' * 32),
])
async def test_rejection_signal_requires_allowlisted_kind_and_matching_request(active, marker, request_id):
    manager, _, scope = active
    runtime = manager.deepagents_runtime
    headers = {'x-moyai-tool-rejection-v1': marker}
    if request_id is not None:
        headers['x-moyai-tool-request-id'] = request_id
    runtime.broker_transport = httpx.MockTransport(lambda request: httpx.Response(
        403, headers=headers, json={'outcome': 'rejected', 'dispatched': False, 'exception_type': 'PrivateExportDenied'}))
    with pytest.raises(ApplicationError) as caught:
        await runtime.broker(scope, '/tools/call', {
            'name': 'workspace_diagnostics', 'arguments': {}, 'input_id': scope.message_id, 'request_id': 'a' * 32})
    assert caught.value.type == 'BrokerRejected'


async def test_tool_signal_cannot_reclassify_other_broker_routes(active):
    manager, _, scope = active
    runtime = manager.deepagents_runtime
    runtime.broker_transport = httpx.MockTransport(lambda request: httpx.Response(403, headers={
        'x-moyai-tool-rejection-v1': 'PrivateExportDenied', 'x-moyai-tool-request-id': 'a' * 32}))
    with pytest.raises(ApplicationError) as caught:
        await runtime.broker(scope, '/agent-runtime', {'request_id': 'a' * 32})
    assert caught.value.type == 'BrokerRejected'
