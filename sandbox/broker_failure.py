"""Small, body-free diagnostics shared by the broker and sandbox relay."""
import http.client
import json
from pathlib import Path
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
FAILURE_STAGES = {
    'tool_catalog', 'tool_activity', 'tool_dispatch', 'computer_poll', 'computer_step',
    'tool_receipt', 'tool_journal', 'supervisor_launch', 'supervisor_events', 'supervisor_wait',
    'remote_setup', 'remote_prepare', 'remote_finish', 'remote_activate', 'remote_attachments',
    'remote_dispatch', 'remote_result', 'remote_cleanup',
}
SOURCE_ROOT = Path(__file__).resolve().parents[1]
STACK_LOCATION = re.compile(r'(?:app|agent|sandbox)/(?:[a-zA-Z0-9_]+/)*[a-zA-Z0-9_]+\.py:[1-9][0-9]{0,5}:[A-Za-z_][A-Za-z0-9_]{0,79}')


def exception_diagnostic(exc, *, stage, request_id=None):
    """Capture structure, never messages, source lines, arguments or locals.

    Only our installed code contributes stack locations. Third-party frames,
    absolute paths and exception/provider prose never cross this boundary.
    """
    try:
        return _exception_diagnostic(exc, stage=stage, request_id=request_id)
    except Exception:
        # Even hostile exception properties or unavailable filesystems cannot
        # replace the original failure with a telemetry error.
        return safe_error({'exception_type': type(exc).__name__, 'failure_stage': stage, 'request_id': request_id})


def _exception_diagnostic(exc, *, stage, request_id):
    result = {'failure_stage': stage, 'request_id': request_id}
    chain, locations, seen = [], [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        name = type(exc).__name__
        # Keep this shared sandbox module independent of the Temporal SDK.
        # Only the known SDK envelope has semantic types/status details;
        # never infer either from arbitrary exception text or details.
        if name == 'ApplicationError' and type(exc).__module__ == 'temporalio.exceptions':
            name = safe_error({'exception_type': exc.type}).get('exception_type', name)
            if name == 'BrokerRejected' and exc.details:
                for key, value in safe_error({'http_status': exc.details[0]}).items():
                    result.setdefault(key, value)
        chain.append(name)
        if 'exception_type' not in result:
            result['exception_type'] = name
        if type(getattr(exc, 'errno', None)) is int:
            result.setdefault('errno', exc.errno)
        if type(getattr(exc, 'status_code', None)) is int:
            result.setdefault('http_status', exc.status_code)
        if type(getattr(exc, 'returncode', None)) is int:
            result.setdefault('exit_code', exc.returncode)
        tb = exc.__traceback__
        frames = []
        while tb:
            code = tb.tb_frame.f_code
            try:
                relative = Path(code.co_filename).resolve().relative_to(SOURCE_ROOT).as_posix()
            except (ValueError, OSError):
                relative = ''
            location = f'{relative}:{tb.tb_lineno}:{code.co_name}'
            if STACK_LOCATION.fullmatch(location):
                frames.append(location)
            tb = tb.tb_next
        locations.extend(frames[-8:])
        # __context__ remains valuable even for "raise ... from None"; copy
        # only structure, never resurrect its suppressed private message.
        exc = exc.__cause__ or exc.__context__
    result.update(exception_chain=chain, stack_locations=locations[-12:])
    if len(chain) > 1:
        result['cause_type'] = chain[1]
    return safe_error(result)


def safe_error(data):
    """One additive, body-free projection for traces, events and diagnostics."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, values in {'error_code': ERROR_CODES, 'stage': ERROR_STAGES, 'sdk_error': CLAUDE_ERRORS,
                        'route': BROKER_ROUTES, 'code': CODEX_ERRORS, 'native_status': CLAUDE_RESULTS,
                        'terminal_reason': CLAUDE_TERMINAL_REASONS,
                        'failure_stage': FAILURE_STAGES,
                        'outcome_type': {'UncertainToolOutcome'}}.items():
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
    if data.get('failure_stage'):
        # Every component has already passed the body-free projection.
        facts = [data.get('exception_type', 'OperationError'), data['failure_stage']]
        if data.get('errno') is not None:
            facts.append('errno ' + str(data['errno']))
        if data.get('exit_code') is not None:
            facts.append('exit ' + str(data['exit_code']))
        return 'Operation failed (' + '; '.join(facts) + ').'
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
        **safe_error({'error_code': headers.get('x-moyai-error-code'), 'stage': headers.get('x-moyai-error-stage')}),
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
