"""Correlate broker boundaries without logging capabilities or request bodies."""
import json
import logging
import re
import time
from uuid import uuid4

from fastapi import HTTPException
import httpx

from sandbox.broker_failure import BROKER_ROUTES, http_status, request_ids


logger = logging.getLogger('uvicorn.error.moyai.broker')


def model_gateway_error(request_id: str, upstream: httpx.Response, raw_error: bytes | bytearray) -> HTTPException:
    """Keep terminal HTTP errors terminal without publishing provider payloads."""
    reasons = {
        'cyber_policy': 'The model provider rejected this request under its cybersecurity policy',
        'content_policy_violation': 'The model provider rejected this request under its content policy',
    }
    code = ''
    try:
        value = json.loads(raw_error[:8192])
        error = value.get('error') if isinstance(value, dict) else None
        # LiteLLM can put the provider's JSON in error.message, with a numeric
        # HTTP status in error.code. Decode one envelope, never match prose or
        # forward its message, which can contain input, keys and private URLs.
        for attempt in range(2):
            if not isinstance(error, dict):
                break
            candidate = error.get('code')
            if isinstance(candidate, str) and candidate in reasons:
                code = candidate
                break
            message = error.get('message')
            prefix = 'litellm.BadRequestError: OpenAIException - '
            if attempt or not isinstance(message, str) or not message.startswith(prefix):
                break
            nested, _ = json.JSONDecoder().raw_decode(message[len(prefix):].lstrip())
            error = nested.get('error') if isinstance(nested, dict) else None
    except (ValueError, RecursionError):
        pass
    status = upstream.status_code
    fallback = {
        400: 'The model gateway rejected an invalid request',
        401: 'The model gateway rejected authentication',
        403: 'The model gateway denied access to this request',
        404: 'The model gateway could not find the requested model or endpoint',
        422: 'The model gateway could not process this request',
        429: 'The model gateway rate-limited this request',
    }
    reason = reasons[code] if code else fallback.get(status, 'The model gateway request failed')
    detail = f'{reason} ({code + "; " if code else ""}HTTP {status}).'
    return HTTPException(status, detail, headers=upstream_headers(request_id, upstream))


def upstream_headers(request_id, upstream=None, *, error=None):
    headers = {'X-Moyai-Model-Request-ID': request_id}
    if error is not None:
        headers['X-Moyai-Upstream-Error-Type'] = type(error).__name__
    if upstream is not None:
        headers['X-Moyai-Upstream-Status'] = str(upstream.status_code)
        headers.update({name: value for name, value in request_ids(upstream.headers).items()
                        if name not in {'x-moyai-request-id', 'x-moyai-model-request-id'}})
    return headers


class BrokerDiagnosticsMiddleware:
    """ASGI logging includes stream completion/failure, after response headers."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        match = re.fullmatch(r'/broker/([0-9a-f]{32})(/.*)', scope.get('path', ''))
        if scope['type'] != 'http' or not match or match[2] not in BROKER_ROUTES:
            return await self.app(scope, receive, send)
        headers = {key.decode('latin1').lower(): value.decode('latin1') for key, value in scope['headers']}
        supplied = headers.get('x-moyai-request-id', '')
        correlation = supplied if re.fullmatch(r'[0-9a-f]{32}', supplied) else uuid4().hex
        fields = {'version': 1, 'run_id': match[1], 'route': match[2],
                  'request_id': correlation, 'method': scope['method']}
        started = time.monotonic()
        status, response_bytes, response_started = None, 0, False
        response_ids, upstream_status, upstream_error_type = {}, None, ''
        logger.info(json.dumps({'event': 'broker_request_started', **fields}))

        async def observed_send(message):
            nonlocal status, response_bytes, response_started, response_ids, upstream_status, upstream_error_type
            if message['type'] == 'http.response.start':
                status, response_started = message['status'], True
                raw = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'x-moyai-request-id']
                raw.append((b'x-moyai-request-id', correlation.encode()))
                message = {**message, 'headers': raw}
                reply = {key.decode('latin1').lower(): value.decode('latin1') for key, value in raw}
                response_ids = request_ids(reply)
                upstream_status = http_status(reply.get('x-moyai-upstream-status'))
                upstream_error_type = reply.get('x-moyai-upstream-error-type', '')
            elif message['type'] == 'http.response.body':
                response_bytes += len(message.get('body', b''))
            await send(message)

        error_type = ''
        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            logger.info(json.dumps({
                'event': 'broker_request_failed' if error_type or (status and status >= 400) else 'broker_request_finished',
                **fields, 'http_status': status, 'upstream_status': upstream_status,
                'request_ids': response_ids, 'response_started': response_started,
                'response_bytes': response_bytes, 'error_type': error_type,
                'upstream_error_type': upstream_error_type,
                'duration_ms': round((time.monotonic() - started) * 1000),
            }))
