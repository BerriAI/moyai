"""Supplementary projection/receipt tests; not real-provider deployment proof."""
import json

import pytest

from agent.activity import ActivityReporter
from app.activity_history import history_page
from app.db import Store
from app.failure_explanations import explain, project_event, recovery_record
from sandbox.broker_failure import tool_evidence
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app, traced_tool_runtime  # noqa: F401
from test_deepagents_receipts import broker


@pytest.mark.parametrize('data,category', [
    ({'phase': 'sdk_failure', 'exception_type': 'TimeoutError'}, 'execution'),
    ({'exception_type': 'ModelResponseIncomplete'}, 'no-answer'),
    ({'tool': 'github_checkout', 'diagnostic': {'exception_type': 'ValueError'}}, 'checkout'),
    ({'tool': 'slack_send'}, 'slack-delivery'),
    ({'tool': 'artifact_publish'}, 'artifact-path'),
    ({'tool': 'media_share'}, 'capture-persistence'),
    ({'tool': 'slack_file_read'}, 'attachment'),
    ({'exception_type': 'PrivateExportDenied'}, 'private-export'),
    ({'exception_type': 'ContextImportIncomplete'}, 'checkpoint'),
    ({'sdk_error': 'invalid_request', 'error_code': 'unknown', 'native_status': 'success'}, 'invalid-request'),
    ({'error_code': 'cyber_policy', 'http_status': 400}, 'provider-policy'),
    ({'tool': 'github_ci_checks', 'diagnostic': {'http_status': 403}}, 'github-access'),
])
def test_categories_describe_observed_boundary_not_inferred_root_cause(data, category):
    result = explain(data)
    assert result['category'] == category
    assert result['action'] == {'dispatched': None, 'outcome': 'unknown'}
    assert result['recovery'] is None
    if category in {'execution', 'checkout', 'slack-delivery', 'artifact-path', 'capture-persistence', 'attachment', 'github-access'}:
        assert 'Underlying cause unknown.' in result['observed_cause']
    assert 'database' not in result['observed_cause'].lower()


def test_prose_and_arbitrary_fields_cannot_supply_cause_retry_or_support():
    secret = 'sk-veryprivate123'
    value = {'phase': 'tool_failure', 'error': 'cyber_policy', 'message': 'checkpoint missing',
             'exception_type': secret, 'request_id': secret, 'activity_type': {'PRIVATE': secret},
             'recovery': {'method': 'catalog_read', 'decision': 'exhausted', 'attempts': secret},
             'action': {'dispatched': 'false', 'outcome': ['completed'], 'retryable': 'true'},
             'diagnostic': {'body': secret, 'stack': secret}, 'input': secret, 'output': secret}
    result = explain(value)
    assert result['observed_cause'] == 'Underlying cause unknown.'
    assert result['category'] == 'unknown'
    assert result['action'] == {'dispatched': None, 'outcome': 'unknown'}
    assert result['retryability'] == 'bounded_recovery_stopped'
    assert secret not in json.dumps(result)
    assert result['recovery'] == {'method': 'catalog_read', 'decision': 'exhausted'}
    for value in [None, [], {'method': [], 'decision': 'recovered'}, {'method': 'catalog_read', 'decision': 'PRIVATE'}]:
        assert recovery_record(value) is None


def test_unknown_write_cannot_become_retryable_and_confirmed_receipt_stays_confirmed():
    uncertain = explain({'action': {'dispatched': True, 'outcome': 'unknown', 'retryable': True}})
    assert uncertain['retryability'] == 'blocked_unknown_outcome'
    confirmed = explain({'failure_stage': 'tool_journal', 'exception_type': 'CapabilityExpired',
                         'action': {'dispatched': True, 'outcome': 'completed', 'retryable': True}})
    assert confirmed['action']['outcome'] == 'completed'
    assert confirmed['retryability'] == 'do_not_repeat'
    assert 'Saving conversation history' == confirmed['stage']


def test_broker_receipt_metadata_survives_native_activity_without_trusting_computer_field():
    events = []
    reporter = ActivityReporter(lambda kind, message, data: events.append((kind, data)))
    result = {'isError': True, 'error': 'PRIVATE', 'outcome': 'unknown', 'dispatched': True,
              'retryable': False, 'receipt_status': 'completed', 'status_code': 503,
              'diagnostic': {'exception_type': 'ReadTimeout', 'response_body': 'PRIVATE'}}
    reporter.start('call', 'credentials_run', {'token': 'PRIVATE'})
    reporter.complete('call', 'credentials_run', {}, result)
    data = events[-1][1]
    explanation = explain(data)
    assert explanation['action'] == {'dispatched': True, 'outcome': 'unknown'}
    assert explanation['retryability'] == 'blocked_unknown_outcome'
    assert 'PRIVATE' not in json.dumps(explanation)
    assert tool_evidence({'receipt_status': 'completed'}) == {}


def test_stream_and_deferred_history_share_projection_and_keep_original_turn(tmp_path):
    store = Store(tmp_path)
    run = store.create_run('Task', '', 'demo', [], chat_enabled=True)
    first = store.claim_message(run['id'])
    second, _ = store.enqueue_message(run['id'], 'Next', 'next')
    store.execute('UPDATE runs SET active_message_id=? WHERE id=?', (second['id'], run['id']))
    store.event(run['id'], 'tool', 'PRIVATE output', {
        'phase': 'error', 'activity_version': 1, 'call_id': 'identity:1', 'tool': 'github_checkout',
        'diagnostic': {'exception_type': 'ValueError'}, 'output': 'PRIVATE',
        'failure_explanation': {'observed_cause': 'PRIVATE'}}, turn_id=first['id'])
    streamed = next(e for e in store.events(run['id']) if e['kind'] == 'tool')
    deferred = next(e for e in history_page(store, run['id'], first['id'])['events'] if e['kind'] == 'tool')
    assert streamed == deferred
    assert streamed['data']['turn_id'] == first['id']
    assert 'PRIVATE' not in json.dumps(streamed['failure_explanation'])
    assert not [e for e in history_page(store, run['id'], second['id'])['events'] if e['kind'] == 'tool']
    assert project_event({**streamed, 'failure_explanation': {'observed_cause': 'forged'}})['failure_explanation'] == streamed['failure_explanation']
    store.close()


async def test_confirmed_computer_receipt_survives_later_journal_failure(traced_tool_runtime, monkeypatch):
    probe = traced_tool_runtime
    calls = broker(probe)
    def fail(*args):
        raise RuntimeError('PRIVATE journal failure')
    monkeypatch.setattr(probe.runtime, 'append', fail)
    with pytest.raises(RuntimeError):
        await probe.runtime._call_tool(probe.scope, 'confirmed', 'credentials_run', {})
    assert len(calls) == 1
    event = next(e for e in probe.store.events(probe.scope.run_id) if e['data'].get('phase') == 'tool_failure')
    assert event['failure_explanation']['action']['outcome'] == 'completed'
    assert event['failure_explanation']['retryability'] == 'do_not_repeat'
    assert 'PRIVATE' not in json.dumps(event['failure_explanation'])


async def test_catalog_recovery_records_actual_attempts_without_tool_dispatch(active):
    import httpx
    manager, store, scope = active
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) < 3 else 200, json=[] if len(calls) == 3 else {})
    manager.deepagents_runtime.broker_transport = httpx.MockTransport(respond)
    await manager.deepagents_runtime.catalog(scope)
    records = [e for e in store.events(scope.run_id) if e['data'].get('phase') == 'recovery_decision']
    assert [e['data']['recovery']['attempts'] for e in records] == [3]
    assert [e['data']['recovery']['decision'] for e in records] == ['recovered']
    assert all(e['data']['turn_id'] == scope.message_id for e in records)
    assert all(request.method == 'GET' for request in calls)


def test_slack_contract_is_optional_and_never_infers_missing_bytes_or_http_counts():
    data = {'phase': 'slack_attachment_read', 'code': 'metadata_size_mismatch', 'stage': 'download',
            'attempts': 2, 'declared_bytes': 10, 'received_bytes': 12, 'http_status': 200,
            'remaining_blocker': 'PRIVATE', 'next_action': 'PRIVATE', 'recovery_tried': 'PRIVATE',
            'attempt_history': [{'code': 'PRIVATE', 'url': 'PRIVATE'}]}
    value = explain(data)
    assert value['category'] == 'attachment'
    assert 'cause of the disagreement is unknown' in value['observed_cause']
    assert value['measurements'] == {'declared_bytes': 10, 'received_bytes': 12}
    assert value['recovery'] == {'method': 'attachment_read', 'attempts': 2, 'decision': 'unknown'}
    assert 'PRIVATE' not in json.dumps(value)
    for invalid in [None, True, -1, float('nan'), float('inf'), 2**54, '12']:
        value = explain({**data, 'received_bytes': invalid})
        assert 'received_bytes' not in value['measurements']
    assert explain({'phase': 'slack_attachment_read', 'code': 'future_code'})['observed_cause'] == 'Underlying cause unknown.'


def test_capture_copy_warning_preserves_completed_capture_without_requesting_recapture():
    value = explain({'stage': 'capture_sync', 'status': 'unconfirmed', 'request_id': 'capture-1',
                     'error_type': 'PRIVATE', 'status_code': 503})
    assert value['category'] == 'capture-persistence'
    assert value['action'] == {'dispatched': True, 'outcome': 'completed'}
    assert value['retryability'] == 'do_not_repeat'
    assert 'Do not repeat the capture' in value['next_action']
    assert 'PRIVATE' not in json.dumps(value)


@pytest.mark.parametrize('code', ['credential_upstream', 'credential_transport',
                                 'credential_unconfirmed', 'credential_unavailable'])
def test_slack_credential_failures_do_not_infer_denial_or_authorize_refresh_replay(code):
    data = {'phase': 'slack_attachment_read', 'stage': 'access', 'code': code, 'attempts': 1,
            'message': 'PRIVATE provider says authentication failed', 'remaining_blocker': 'PRIVATE',
            'next_action': 'PRIVATE replay this POST', 'recovery_tried': 'PRIVATE'}
    if code == 'credential_upstream':
        data['http_status'] = 503
    value = explain(data)
    assert value['component'] == 'Slack credential acquisition'
    assert value['stage'] == 'Obtaining Slack credentials'
    assert value['retryability'] == 'credential_replay_blocked'
    assert value['recovery'] == {'method': 'attachment_read', 'attempts': 1, 'decision': 'credential_replay_blocked'}
    assert 'not automatically replay' in value['next_action']
    assert 'authentication failed' not in value['observed_cause']
    assert 'Restore the required Slack access' not in value['next_action']
    assert value['measurements'] == {} and 'representation' not in value
    assert 'PRIVATE' not in json.dumps(value)
    assert value['action'] == ({'dispatched': False, 'outcome': 'failed'} if code == 'credential_transport'
                               else {'dispatched': None, 'outcome': 'unknown'})
    confirmed = explain({**data, 'action': {'dispatched': True, 'outcome': 'completed'}})
    assert confirmed['action'] == {'dispatched': True, 'outcome': 'completed'}
    assert confirmed['retryability'] == 'do_not_repeat'


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'br', 'deflate', 'zstd', 'unsupported'])
@pytest.mark.parametrize('present', [True, False, None])
def test_slack_encoding_is_allowlisted_and_presence_is_not_invented(encoding, present):
    data = {'phase': 'slack_attachment_read', 'code': 'encoded_response', 'stage': 'headers',
            'content_encoding': encoding}
    if present is not None:
        data['content_encoding_present'] = present
    value = explain(data)
    assert value['representation'] == {'content_encoding': encoding,
        **({'content_encoding_present': present} if present is not None else {})}
    assert 'received_bytes' not in value['measurements']


@pytest.mark.parametrize('encoding,present', [('gzip PRIVATE', 'false'), (['identity'], 0), ({'PRIVATE': 1}, 1)])
def test_slack_encoding_rejects_raw_headers_and_malformed_presence(encoding, present):
    value = explain({'phase': 'slack_attachment_read', 'code': 'encoded_response',
                     'content_encoding': encoding, 'content_encoding_present': present})
    assert 'representation' not in value
    assert 'PRIVATE' not in json.dumps(value)
    assert 'representation' not in explain({'phase': 'slack_attachment_read', 'stage': 'access'})


def test_empty_completed_result_records_once_without_changing_lifecycle_or_guessing_stale_turn(active):
    manager, store, scope = active
    result = {'message_id': scope.message_id, 'completed': True, 'message': ''}
    original_status = store.run(scope.run_id)['status']
    manager.receive_result(scope.run_id, result)
    manager.receive_result(scope.run_id, {**result, 'checkpoint_saved': True})
    rows = [e for e in store.events(scope.run_id) if e['data'].get('phase') == 'response_completion']
    assert len(rows) == 1
    assert rows[0]['data']['turn_id'] == scope.message_id
    assert rows[0]['failure_explanation']['category'] == 'no-answer'
    assert 'supplied no final answer text' in rows[0]['failure_explanation']['observed_cause']
    assert store.run(scope.run_id)['status'] == original_status
    assert json.loads(store.run(scope.run_id)['pending_result'])['completed'] is True
    store.execute('DELETE FROM events WHERE run_id=?', (scope.run_id,))
    manager.receive_result(scope.run_id, {**result, 'message_id': scope.message_id + 1})
    assert not [e for e in store.events(scope.run_id) if e['data'].get('phase') == 'response_completion']


def test_agent_diagnostics_preserves_turn_and_does_not_grant_private_export():
    from app.workspace_diagnostics import failure_record
    value = failure_record({'id': 1, 'created_at': '2026-10-13', 'data': json.dumps({
        'phase': 'tool_failure', 'turn_id': 44, 'exception_type': 'PrivateExportDenied',
        'failure_stage': 'tool_dispatch', 'action': {'dispatched': False, 'outcome': 'rejected'}})})
    assert value['turn_id'] == 44
    assert value['explanation']['category'] == 'private-export'
    assert 'not a grantable permission' in value['explanation']['next_action']
    assert 'product fix' in value['explanation']['next_action']


def test_native_empty_completion_is_observed_before_fallback_prose(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent import agent
    events = []
    monkeypatch.setattr(agent, 'create_agent', lambda *args, **kwargs: SimpleNamespace(
        validate=lambda: None, close=lambda: None,
        run_conversation=lambda *args, **kwargs: {'completed': True, 'final_response': ''}))
    result = agent.run({'run_id': 'empty-completion', 'prompt': 'Task', 'harness': 'codex'},
        relay=SimpleNamespace(last_error='', wait_group='', wait_credential=''),
        workspace=tmp_path, session=tmp_path / 'session', config={},
        emit=lambda kind, text, data=None, **extra: events.append((kind, data, extra)))
    observations = [data for kind, data, _ in events if kind == 'error' and data and data.get('phase') == 'response_completion']
    assert observations == [{'phase': 'response_completion', 'code': 'missing_final_answer',
                             'completed_marker': True, 'final_text_present': False}]
    final = next(extra for kind, _, extra in events if kind == 'final')
    assert final['completed'] is True
    assert result['exit_code'] == 0


def test_model_rejection_keeps_safe_http_category_and_ids_without_provider_body():
    from temporalio.exceptions import ApplicationError
    from app.deepagents_diagnostics import workflow_failure
    from test_deepagents_failures import workflow_failure as wrap
    error = ApplicationError('PRIVATE', {
        'http_status': 401, 'error_code': 'authentication_error', 'stage': 'upstream',
        'request_ids': {'x-request-id': 'request-401', 'Authorization': 'PRIVATE'},
        'provider_body': 'PRIVATE'}, type='ModelRequestFailed', non_retryable=True)
    diagnostic = workflow_failure(wrap(error, activity_type='deepagents.invoke_model'))
    result = explain(diagnostic)
    assert diagnostic['http_status'] == 401
    assert diagnostic['request_ids'] == {'x-request-id': 'request-401'}
    assert result['retryability'] == 'not_retryable'
    assert 'authentication failure' in result['observed_cause']
    assert 'PRIVATE' not in json.dumps(diagnostic)
