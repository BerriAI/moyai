"""Small, body-free diagnostics shared by the broker and sandbox relay."""
import http.client
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
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


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
