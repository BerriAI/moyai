"""Correlate broker boundaries without logging capabilities or request bodies."""
import asyncio
import json
import logging
import re
import time
from uuid import uuid4

from fastapi import HTTPException
import httpx

from sandbox.broker_failure import BROKER_ROUTES, MODEL_ROUTES, http_status, request_ids, provider_error, safe_error, error_summary


logger = logging.getLogger('uvicorn.error.moyai.broker')


def model_gateway_error(request_id: str, upstream: httpx.Response, raw_error: bytes | bytearray) -> HTTPException:
    """Keep terminal HTTP errors terminal without publishing provider payloads."""
    reasons = {
        'cyber_policy': 'The model provider rejected this request under its cybersecurity policy',
        'content_policy_violation': 'The model provider rejected this request under its content policy',
    }
    try:
        code = provider_error(json.loads(raw_error[:8192]))
    except (ValueError, RecursionError):
        code = 'unknown'
    status = upstream.status_code
    fallback = {
        400: 'The model gateway rejected an invalid request',
        401: 'The model gateway rejected authentication',
        403: 'The model gateway denied access to this request',
        404: 'The model gateway could not find the requested model or endpoint',
        422: 'The model gateway could not process this request',
        429: 'The model gateway rate-limited this request',
    }
    reason = reasons.get(code, fallback.get(status, 'The model gateway request failed'))
    detail = f'{reason} ({code + "; " if code != "unknown" else ""}HTTP {status}).'
    return HTTPException(status, detail, headers={**upstream_headers(request_id, upstream),
        'X-Moyai-Error-Code': code, 'X-Moyai-Error-Stage': 'upstream'})


def upstream_headers(request_id, upstream=None, *, error=None):
    headers = {'X-Moyai-Model-Request-ID': request_id}
    if error is not None:
        headers['X-Moyai-Upstream-Error-Type'] = type(error).__name__
    if upstream is not None:
        headers['X-Moyai-Upstream-Status'] = str(upstream.status_code)
        headers.update({name: value for name, value in request_ids(upstream.headers).items()
                        if name not in {'x-moyai-request-id', 'x-moyai-model-request-id'}})
    return headers


def model_error(exc=None, *, stage='upstream', status=None, headers=None, code='unknown'):
    if isinstance(exc, asyncio.CancelledError):
        code = 'cancelled'
    headers = httpx.Headers(headers or getattr(exc, 'headers', None) or {})
    return safe_error({'error_code': headers.get('x-moyai-error-code', code),
        'stage': headers.get('x-moyai-error-stage', stage),
        'http_status': status or getattr(exc, 'status_code', None),
        'upstream_status': http_status(headers.get('x-moyai-upstream-status')),
        'exception_type': type(exc).__name__ if exc is not None else None,
        'cause_type': type(exc.__cause__).__name__ if exc is not None and exc.__cause__ is not None else None,
        'request_ids': request_ids(headers)})


def model_stage(request, stage, *, request_id=None, error=None):
    observation = request.scope.get('moyai_model_diagnostic')
    if observation is not None:
        observation['stage'] = stage
        if request_id:
            observation['model_request_id'] = request_id
        if error:
            observation['error'] = safe_error(error)


class BrokerDiagnosticsMiddleware:
    """ASGI logging includes stream completion/failure, after response headers."""
    def __init__(self, app, store=None, tracing=None):
        self.app, self.store, self.tracing = app, store, tracing

    async def __call__(self, scope, receive, send):
        match = re.fullmatch(r'/broker/([0-9a-f]{32})(/.*)', scope.get('path', ''))
        if scope['type'] != 'http' or not match or match[2] not in BROKER_ROUTES:
            return await self.app(scope, receive, send)
        headers = {key.decode('latin1').lower(): value.decode('latin1') for key, value in scope['headers']}
        supplied = headers.get('x-moyai-request-id', '')
        correlation = supplied if re.fullmatch(r'[0-9a-f]{32}', supplied) else uuid4().hex
        fields = {'version': 1, 'run_id': match[1], 'route': match[2],
                  'request_id': correlation, 'method': scope['method']}
        observation = {}
        if match[2] in MODEL_ROUTES:
            scope['moyai_model_diagnostic'] = observation
        trace_started = time.time_ns()
        started = time.monotonic()
        status, response_bytes, response_started = None, 0, False
        first_response_body_ms = None
        response_ids, upstream_status, upstream_error_type = {}, None, ''
        logger.info(json.dumps({'event': 'broker_request_started', **fields}))

        async def observed_send(message):
            nonlocal status, response_bytes, response_started, response_ids, upstream_status, upstream_error_type, first_response_body_ms, reply
            if message['type'] == 'http.response.start':
                status, response_started = message['status'], True
                raw = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'x-moyai-request-id']
                raw.append((b'x-moyai-request-id', correlation.encode()))
                if status >= 400 and match[2] in MODEL_ROUTES:
                    metadata = safe_error({'stage': observation.get('stage', 'validation'),
                                           'error_code': 'unknown', **observation.get('error', {})})
                    existing = {key.lower() for key, _ in raw}
                    for field, header in [('stage', b'x-moyai-error-stage'), ('error_code', b'x-moyai-error-code')]:
                        if header not in existing and field in metadata:
                            raw.append((header, metadata[field].encode()))
                message = {**message, 'headers': raw}
                reply = {key.decode('latin1').lower(): value.decode('latin1') for key, value in raw}
                response_ids = request_ids(reply)
                upstream_status = http_status(reply.get('x-moyai-upstream-status'))
                upstream_error_type = reply.get('x-moyai-upstream-error-type', '')
            elif message['type'] == 'http.response.body':
                if message.get('body') and first_response_body_ms is None:
                    first_response_body_ms = round((time.monotonic() - started) * 1000, 2)
                response_bytes += len(message.get('body', b''))
            await send(message)

        error_type = ''
        exception = None
        reply = {}
        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            error_type = type(exc).__name__
            exception = exc
            raise
        finally:
            failed = error_type or (status and status >= 400) or observation.get('error')
            diagnostic = {}
            if failed and match[2] in MODEL_ROUTES:
                diagnostic = {**model_error(exception, stage=observation.get('stage', 'validation'),
                    status=status, headers=reply), **observation.get('error', {}),
                    'route': match[2], 'request_id': correlation}
                if observation.get('model_request_id'):
                    diagnostic['model_request_id'] = observation['model_request_id']
                run = observation.get('run')
                # Only a successfully authenticated request may publish into a
                # run. Freeze its original turn, even if a later turn is claimed.
                if run is not None and self.store is not None:
                    try:
                        self.store.event(run['id'], 'error', error_summary(diagnostic),
                            {'phase': 'broker_failure', **diagnostic}, turn_id=run.get('active_message_id') or 0)
                        if not observation.get('model_request_id') and self.tracing is not None:
                            self.tracing.broker(run, correlation, trace_started, diagnostic)
                    except Exception as exc:
                        logger.warning('Broker diagnostic capture failed (%s)', type(exc).__name__)
            logger.info(json.dumps({
                'event': 'broker_request_failed' if failed else 'broker_request_finished',
                **fields, **diagnostic, 'http_status': status, 'upstream_status': upstream_status,
                'request_ids': response_ids, 'response_started': response_started,
                'response_bytes': response_bytes, 'error_type': error_type,
                'upstream_error_type': upstream_error_type,
                'duration_ms': round((time.monotonic() - started) * 1000),
                'first_response_body_ms': first_response_body_ms,
            }))
