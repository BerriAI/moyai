"""Small, body-free diagnostics shared by the broker and sandbox relay."""
import http.client
import json
import re
import urllib.error


MODEL_ROUTES = {'/v1/chat/completions', '/v1/messages', '/v1/responses'}
TOOL_ROUTES = {'/tools/call', '/credentials/materialize', '/credentials/invoke'}
BROKER_ROUTES = MODEL_ROUTES | TOOL_ROUTES | {
    '/tools', '/v1/models', '/control', '/context/native', '/context/window',
    '/context/compact', '/context/maintenance',
}
REQUEST_ID_HEADERS = (
    'x-moyai-request-id', 'x-moyai-model-request-id', 'x-request-id',
    'x-render-request-id', 'rndr-id', 'x-litellm-call-id',
)
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504, 524}
ERROR_CODES = {
    'cyber_policy', 'content_policy_violation', 'authentication_error', 'permission_error',
    'invalid_request_error', 'not_found_error', 'rate_limit_error', 'overloaded_error',
    'api_error', 'server_error', 'context_length_exceeded', 'insufficient_quota',
    'max_output_tokens', 'content_filter', 'stream_incomplete', 'cancelled', 'unknown',
}
TERMINAL_ERROR_CODES = {
    'cyber_policy', 'content_policy_violation', 'authentication_error', 'permission_error',
    'invalid_request_error', 'not_found_error', 'context_length_exceeded', 'insufficient_quota',
    'max_output_tokens', 'content_filter', 'cancelled',
}
ERROR_STAGES = {'validation', 'context_preparation', 'admission', 'upstream', 'stream'}
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


def safe_error(data):
    """One additive, body-free projection for traces, events and diagnostics."""
    result = {}
    for key, values in {'error_code': ERROR_CODES, 'stage': ERROR_STAGES, 'sdk_error': CLAUDE_ERRORS,
                        'route': BROKER_ROUTES, 'code': CODEX_ERRORS, 'native_status': CLAUDE_RESULTS,
                        'terminal_reason': CLAUDE_TERMINAL_REASONS}.items():
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
    if isinstance(data.get('request_ids'), dict):
        result['request_ids'] = request_ids(data['request_ids'])
    return result


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


def error_summary(data, *, technical=True):
    data = safe_error(data)
    code = data.get('error_code', 'unknown')
    reason = {
        'cyber_policy': 'The model provider rejected this request under its cybersecurity policy',
        'content_policy_violation': 'The model provider rejected this request under its content policy',
        'stream_incomplete': 'The model response ended before completion',
        'cancelled': 'The model request was interrupted',
    }.get(code)
    if reason is None:
        reason = {
            'authentication_failed': 'The model provider could not authenticate this request',
            'billing_error': 'The model provider reported a billing problem',
            'rate_limit': 'The model provider rate-limited this request',
            'invalid_request': 'The model provider rejected this request as invalid',
            'server_error': 'The model provider encountered a server error',
        }.get(data.get('sdk_error'), 'Model request failed')
    if not technical:
        return reason + (' (HTTP ' + str(data['http_status']) + ')' if data.get('http_status') else '') + '.'
    facts = [value for value in (code if code != 'unknown' else '', data.get('sdk_error'),
                                 data.get('code'), data.get('native_status'), data.get('terminal_reason'),
                                 data.get('exception_type'), data.get('stage')) if value]
    if data.get('http_status'):
        facts.append('HTTP ' + str(data['http_status']))
    return reason + (' (' + '; '.join(facts) + ')' if facts else ' (cause unavailable)') + '.'


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
    diagnostic = safe_error({'error_code': headers.get('x-moyai-error-code'), 'stage': headers.get('x-moyai-error-stage')})
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
        **diagnostic,
        'version': 1, 'route': route if route in BROKER_ROUTES else 'unknown',
        'http_status': status, 'upstream_status': upstream,
        'request_id': request_id, 'request_ids': request_ids(headers),
        'error_type': type(exc).__name__, 'response_started': bool(response_started),
        'upstream_error_type': upstream_error,
        'cause_type': type(cause).__name__ if cause else '',
        'errno': errno if type(errno) is int else None,
        'response_bytes': response_bytes,
        'transient': diagnostic.get('error_code') not in TERMINAL_ERROR_CODES and (interrupted or (transient and not response_started)),
        'transport_interrupted': interrupted,
        'uncertain_tool': route in TOOL_ROUTES,
    }
