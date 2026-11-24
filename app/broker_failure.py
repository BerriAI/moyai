"""Small, body-free diagnostics shared by the broker and sandbox relay."""
import http.client
import json
import re
import urllib.error


MODEL_ROUTES = {'/v1/chat/completions', '/v1/messages', '/v1/responses'}
TOOL_ROUTES = {'/tools/call', '/credentials/materialize', '/credentials/invoke', '/credentials/aws/writeback'}
BROKER_ROUTES = MODEL_ROUTES | TOOL_ROUTES | {
    '/tools', '/v1/models', '/control', '/context/native', '/context/window',
    '/context/compact', '/context/maintenance',
}
REQUEST_ID_HEADERS = (
    'x-moyai-request-id', 'x-moyai-model-request-id', 'x-request-id',
    'x-render-request-id', 'rndr-id', 'x-litellm-call-id',
)
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504, 524}
TOOL_REJECTION_HEADERS = {'x-moyai-tool-rejection-v1', 'x-moyai-tool-request-id'}
TOOL_REJECTION_TYPES = {'PrivateExportDenied', 'ToolRejected'}
ERROR_CODES = {
    'cyber_policy', 'content_policy_violation', 'authentication_error', 'permission_error',
    'invalid_request_error', 'not_found_error', 'rate_limit_error', 'overloaded_error',
    'api_error', 'server_error', 'context_length_exceeded', 'insufficient_quota',
    'max_output_tokens', 'content_filter', 'stream_incomplete', 'cancelled', 'unknown',
}
ERROR_STAGES = {'validation', 'context_preparation', 'admission', 'upstream', 'stream'}
COMPACTION_REASONS = {
    'invalid_response', 'refused', 'incomplete_output', 'unexpected_tool_call',
    'incomplete_response', 'empty_summary', 'summary_too_large', 'summary_input_too_large',
    'upstream_unavailable', 'upstream_rejected', 'gateway_unreachable',
}
CLAUDE_ERRORS = {'authentication_failed', 'billing_error', 'rate_limit', 'invalid_request', 'server_error', 'unknown'}

CODEX_ERRORS = {
    'contextWindowExceeded', 'sessionBudgetExceeded', 'usageLimitExceeded',
    'rateLimitExceeded', 'flexUnavailable', 'serverOverloaded', 'cyberPolicy',
    'misalignmentPolicyViolation', 'tooManyDenials', 'internalServerError',
    'unauthorized', 'badRequest', 'threadRollbackFailed', 'sandboxError', 'other',
    'httpConnectionFailed', 'responseStreamConnectionFailed',
    'responseStreamDisconnected', 'responseTooManyFailedAttempts',
}
CLAUDE_RESULTS = {'success', 'error_during_execution', 'error_max_turns',
                  'error_max_budget_usd', 'error_max_structured_output_retries'}
CLAUDE_TERMINAL_REASONS = {'completed', 'max_turns', 'max_budget_usd',
                          'aborted_streaming', 'aborted_tools', 'error'}
FAILURE_STAGES = {
    'runtime_entrypoint',
    'model_metadata',
    'tool_catalog', 'tool_activity', 'tool_dispatch', 'computer_poll', 'computer_step',
    'tool_receipt', 'tool_journal', 'supervisor_launch', 'supervisor_events', 'supervisor_wait',
    'remote_setup', 'remote_prepare', 'remote_finish', 'remote_activate', 'remote_attachments',
    'remote_dispatch', 'remote_result', 'remote_cleanup',
}
STACK_LOCATION = re.compile(r'(?:app|agent|sandbox)/(?:[a-zA-Z0-9_]+/)*[a-zA-Z0-9_]+\.py:[1-9][0-9]{0,5}:[A-Za-z_][A-Za-z0-9_]{0,79}')


def safe_error(data):
    """One additive, body-free projection for traces, events and diagnostics."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, values in {'error_code': ERROR_CODES, 'stage': ERROR_STAGES, 'sdk_error': CLAUDE_ERRORS,
                        'compaction_reason': COMPACTION_REASONS,
                        'route': BROKER_ROUTES, 'code': CODEX_ERRORS, 'native_status': CLAUDE_RESULTS,
                        'terminal_reason': CLAUDE_TERMINAL_REASONS,
                        'failure_stage': FAILURE_STAGES,
                        'outcome_type': {'UncertainToolOutcome', 'ToolNotDispatched'}}.items():
        if isinstance(data.get(key), str) and data[key] in values:
            result[key] = data[key]
    for key in ('response_status', 'http_status', 'upstream_status'):
        if type(data.get(key)) is int and 100 <= data[key] <= 599:
            # A successful SSE header describes transport, not the later error.
            result['response_status' if key == 'http_status' and data[key] < 400 else key] = data[key]
    for key in ('request_id', 'model_request_id', 'broker_request_id'):
        if value := safe_id(data.get(key)):
            result[key] = value
    for key in ('exception_type', 'cause_type'):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
            result[key] = value
    for key, lower, upper in (('errno', -4095, 4095), ('exit_code', -255, 255)):
        if type(data.get(key)) is int and lower <= data[key] <= upper:
            result[key] = data[key]
    for key, pattern, limit in (
            ('exception_chain', re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}'), 6),
            ('stack_locations', STACK_LOCATION, 12)):
        if isinstance(data.get(key), list):
            values = [value for value in data[key][:limit] if isinstance(value, str) and pattern.fullmatch(value)]
            if values:
                result[key] = values
    if isinstance(data.get('request_ids'), dict):
        result['request_ids'] = request_ids(data['request_ids'])
    if recovery := safe_recovery(data.get('recovery')):
        result['recovery'] = recovery
    return result


def safe_recovery(value):
    if not isinstance(value, dict):
        return None
    method, decision = value.get('method'), value.get('decision')
    if (method not in ('catalog_read', 'startup', 'model_transport', 'context_transport', 'attachment_read')
            or decision not in ('retrying', 'recovered', 'exhausted', 'deadline', 'not_retryable',
                                'unconfirmed_action', 'unknown', 'retry_after_unavailable', 'retry_after_budget',
                                'credential_replay_blocked')):
        return None
    result = {'method': method, 'decision': decision}
    for key in ('attempts', 'limit', 'scheduled_retries'):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 10000:
            result[key] = number
    return result


def upstream_diagnostic(headers):
    """Decode only our body-free provenance headers, independently of wire status."""
    result = safe_error({
        'upstream_status': http_status(headers.get('x-moyai-upstream-status')),
        'error_code': headers.get('x-moyai-error-code'),
        'stage': headers.get('x-moyai-error-stage'),
        'failure_stage': headers.get('x-moyai-failure-stage'),
        'cause_type': headers.get('x-moyai-upstream-error-type'),
    })
    if ids := request_ids(headers):
        result['request_ids'] = ids
    return result


def tool_rejection_headers(kind, request_id):
    """Only the broker's explicit no-dispatch guards may produce this signal."""
    if (not isinstance(kind, str) or kind not in TOOL_REJECTION_TYPES or not isinstance(request_id, str)
            or not re.fullmatch(r'[0-9a-f]{32}', request_id)):
        return {}
    return {'x-moyai-tool-rejection-v1': kind, 'x-moyai-tool-request-id': request_id}


def tool_evidence(value, *, receipt_status=None):
    """Public receipt fields only; never treat tool prose as an outcome."""
    value = value if isinstance(value, dict) else {}
    action = {}
    for field in ('dispatched', 'retryable'):
        if type(value.get(field)) is bool:
            action[field] = value[field]
    outcome = value.get('outcome')
    if isinstance(outcome, str) and outcome in {'completed', 'failed', 'rejected', 'cancelled', 'unknown'}:
        action['outcome'] = outcome
    if value.get('outcome_uncertain') is True:
        action['outcome'] = 'unknown'
    # Only the computer adapter may supply this argument, never the tool body.
    if isinstance(receipt_status, str) and receipt_status in {'unknown', 'cancelled', 'rejected'}:
        action.update(outcome=receipt_status, retryable=False)
    diagnostic = safe_error(value.get('diagnostic'))
    diagnostic.update(safe_error({'http_status': value.get('status_code')}))
    return {**({'action': action} if action else {}),
            **({'outcome_type': 'UncertainToolOutcome'} if receipt_status == 'unknown' else {}),
            **({'diagnostic': diagnostic} if diagnostic else {})}


def provider_error(value):
    """Decode only known structured envelopes, never infer a cause from prose."""
    error = value.get('error') if isinstance(value, dict) else None
    if not error and isinstance(value, dict):
        response = value.get('response', value)
        if isinstance(response, dict):
            error = response.get('error') or response.get('incomplete_details')
    for attempt in range(2):
        if not isinstance(error, dict):
            break
        for field in ('code', 'type', 'reason'):
            candidate = error.get(field)
            if isinstance(candidate, str) and candidate in ERROR_CODES:
                return candidate
        message = error.get('message')
        prefix = 'litellm.BadRequestError: OpenAIException - '
        if attempt or not isinstance(message, str) or len(message) > 8192 or not message.startswith(prefix):
            break
        try:
            nested, _ = json.JSONDecoder().raw_decode(message[len(prefix):].lstrip())
        except (ValueError, RecursionError):
            break
        error = nested.get('error') if isinstance(nested, dict) else None
    return 'unknown'


def safe_id(value):
    # No whitespace, control characters, arbitrary response headers or bodies.
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', value) else ''


def request_ids(headers):
    return {name: value for name in REQUEST_ID_HEADERS if (value := safe_id(headers.get(name)))}


def http_status(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        return None
    return number if 100 <= number <= 599 else None


def failure(route, request_id, exc, *, headers=None, status=None, response_started=False, response_bytes=0):
    headers = headers or {}
    upstream = http_status(headers.get('x-moyai-upstream-status'))
    effective_status = upstream or status
    # An unmarked local 429 is a run quota, not a temporary provider throttle.
    transient = (effective_status in TRANSIENT_STATUSES if effective_status else True)
    if status == 429 and upstream is None:
        transient = False
    interrupted = (isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException))
                   and not isinstance(exc, urllib.error.HTTPError)
                   and (effective_status is None or 200 <= effective_status < 300))
    upstream_error = headers.get('x-moyai-upstream-error-type', '')
    if not isinstance(upstream_error, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', upstream_error):
        upstream_error = ''
    cause = getattr(exc, 'reason', None)
    if not isinstance(cause, BaseException):
        cause = exc.__cause__
    errno = getattr(cause, 'errno', None) if cause else getattr(exc, 'errno', None)
    return {
        **safe_error({'error_code': headers.get('x-moyai-error-code'), 'stage': headers.get('x-moyai-error-stage'),
                      'failure_stage': headers.get('x-moyai-failure-stage')}),
        'version': 1, 'route': route if route in BROKER_ROUTES else 'unknown',
        'http_status': status, 'upstream_status': upstream,
        'request_id': request_id, 'request_ids': request_ids(headers),
        'error_type': type(exc).__name__, 'response_started': bool(response_started),
        'upstream_error_type': upstream_error,
        'cause_type': type(cause).__name__ if cause else '',
        'errno': errno if type(errno) is int else None,
        'response_bytes': response_bytes,
        'transient': interrupted or (transient and not response_started),
        'transport_interrupted': interrupted,
        'uncertain_tool': route in TOOL_ROUTES,
    }
