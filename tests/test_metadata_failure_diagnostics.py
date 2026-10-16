"""Real app broker/SDK conversion with fixture upstream HTTP, not deployment proof."""
import json
from types import SimpleNamespace
import urllib.error
import urllib.request
from email.message import Message

import httpx
import pytest
from temporalio.api.failure.v1 import Failure
from temporalio.client import WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from app.deepagents_diagnostics import workflow_failure as diagnostic
from app.failure_explanations import explain
from sandbox.startup import read_with_reconnect
from sandbox.broker_failure import exception_diagnostic
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app  # noqa: F401
from test_deepagents_failures import workflow_failure, fail_workflow


@pytest.mark.parametrize('upstream_status,code,terminal', [
    (401, 'authentication_error', True), (403, 'permission_error', True), (500, 'server_error', False),
])
async def test_metadata_status_survives_broker_503_and_temporal_without_secret_body(
        active, durable_app, monkeypatch, upstream_status, code, terminal):
    manager, store, scope = active
    manager.settings.model_context_limits = {}
    manager.settings.litellm_api_base = 'https://gateway.example/v1'
    budget = durable_app.state.context_budget
    budget.cache.clear()
    calls = []
    secret = 'sk-metadata-private-sentinel https://signed.example/private?token=private'

    def upstream(request):
        calls.append(request)
        assert request.url.path == '/model/info'
        return httpx.Response(upstream_status, json={'error': {'code': code, 'message': secret}},
            headers={'x-request-id': 'metadata-request-123', 'Authorization': secret,
                     'x-litellm-call-id': 'sk-private-id-sentinel'})

    actual = httpx.AsyncClient
    monkeypatch.setattr('app.context_budget.httpx', SimpleNamespace(**{
        **vars(httpx), 'AsyncClient': lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw)}))
    # This passes through the actual authenticated app /context/window route.
    with pytest.raises(ApplicationError) as caught:
        await manager.deepagents_runtime.broker(scope, '/context/window')
    assert len(calls) == 1
    assert caught.value.type == 'BrokerRejected'
    assert caught.value.details[0] == 503  # API contract remains unchanged.
    assert caught.value.non_retryable is terminal
    encoded = Failure()
    await DataConverter.default.encode_failure(
        workflow_failure(caught.value, 'deepagents_prepare_v1').cause, encoded)
    decoded = WorkflowFailureError(cause=await DataConverter.default.decode_failure(encoded))
    final = diagnostic(decoded)
    assert final['http_status'] == 503 and final['upstream_status'] == upstream_status
    assert final['failure_stage'] == 'model_metadata'
    assert final['error_code'] == code
    assert final['request_ids']['x-request-id'] == 'metadata-request-123'
    assert final['non_retryable'] is terminal
    events = await fail_workflow(manager, store, scope, decoded)
    assert events[0]['data']['turn_id'] == scope.message_id
    explanation = events[0]['failure_explanation']
    assert explanation['stage'] == 'Reading gateway model metadata'
    if terminal:
        assert f'HTTP {upstream_status}' in explanation['observed_cause']
        assert 'HTTP 503' in explanation['observed_cause']
        assert explanation['retryability'] == 'not_retryable'
        assert 'gateway credentials' in explanation['next_action']
        assert 'MODEL_CONTEXT_LIMITS' not in explanation['next_action']
    serialized = str(encoded) + json.dumps(events) + json.dumps(explain(final))
    assert secret not in serialized
    assert 'signed.example' not in serialized and 'sk-private-id-sentinel' not in serialized
    assert 'Authorization' not in serialized


@pytest.mark.parametrize('upstream', [401, 403])
def test_native_startup_does_not_retry_terminal_metadata_access_hidden_by_503(upstream):
    elapsed, calls = [0], []
    headers = Message()
    headers['X-Moyai-Upstream-Status'] = str(upstream)
    headers['X-Moyai-Failure-Stage'] = 'model_metadata'

    def open_request(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 503, 'safe wrapper', headers, None)

    with pytest.raises(urllib.error.HTTPError) as caught:
        read_with_reconnect(urllib.request.Request('http://127.0.0.1/context/window'), lambda response: None,
            stage='context_window', opener=open_request, clock=lambda: elapsed[0],
            sleep=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    assert caught.value.code == 503
    assert len(calls) == 1 and elapsed[0] == 0
    native = exception_diagnostic(caught.value, stage='runtime_entrypoint')
    assert native['http_status'] == 503 and native['upstream_status'] == upstream
    assert native['failure_stage'] == 'model_metadata'
    assert explain(native)['retryability'] == 'not_retryable'
