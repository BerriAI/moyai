"""Loopback OpenAI/MCP adapter; never forwards a model-chosen destination."""
import hmac
import http.client
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import nullcontext

try:
    from .broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from .broker_failure import MODEL_ROUTES, failure
    from .startup import StartupUnavailable, read_with_reconnect
except ImportError:  # Loaded by the sandbox script, outside a Python package.
    from broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from broker_failure import MODEL_ROUTES, failure
    from startup import StartupUnavailable, read_with_reconnect

EDGE_ERROR = ('Moyai could not reach the model because the cloud connection rejected the request. '
              'Your conversation and files are saved. An administrator needs to repair the connection; '
              'resending the same message will not fix it.')


class BrokerRelay:
    def __init__(self, remote, token, notify=None, report_error=None):
        self.last_error = ''
        self.last_failure = None
        self.uncertain_tool = False
        self.model_failed = False
        self.wait_group = ''
        self.wait_credential = ''
        self.remote, self.token = remote, token
        self.startup_failure = None
        self.steering = None
        self.before_model = None
        self.context_required = None
        self.context_recovery = False
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log capabilities, prompts or tool arguments.

            def error(self, status, message, code='broker_error'):
                content = json.dumps({'error': {'message': message, 'type': code, 'code': code}}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                try:
                    self.wfile.write(content)
                except (ConnectionError, http.client.HTTPException):
                    pass  # The local SDK may have already abandoned its request.

            def handle_request(self):
                steering, generation = None, None
                route, request_id = '', ''
                response_started, response_bytes = False, 0
                response_headers, response_status = {}, None
                request_started = time.monotonic()

                def record_failure(exc, *, headers=None, status=None):
                    diagnostic = failure(route, request_id, exc, headers=headers,
                        status=status, response_started=response_started, response_bytes=response_bytes)
                    diagnostic.update(occurred_at=time.time(), duration_ms=round((time.monotonic() - request_started) * 1000))
                    relay.last_failure = diagnostic
                    relay.uncertain_tool = relay.uncertain_tool or diagnostic['uncertain_tool']
                    if route in MODEL_ROUTES:
                        relay.model_failed = True
                    if report_error:
                        report_error(diagnostic)
                if self.path == '/v1/messages?beta=true':
                    self.path = '/v1/messages'
                claude_request = self.path == '/v1/messages'

                credential_route = re.fullmatch(r'/credentials/([0-9a-f]{32})/v1(/models|/chat/completions|/completions|/embeddings|/messages)',self.path)
                authorized = hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)
                if credential_route or claude_request:
                    authorized = authorized or hmac.compare_digest(self.headers.get('x-api-key',''),token)
                if not authorized:
                    return self.error(401, 'Invalid cloud session capability.')
                allowed = {'GET': {'/v1/models', '/tools'}, 'POST': {'/v1/chat/completions', '/v1/messages', '/v1/responses', '/tools/call', '/credentials/materialize'}}
                if not credential_route and self.path not in allowed.get(self.command, set()):
                    return self.error(404, 'Unknown broker route.')
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if size < 0 or size > body_limit(self.path):
                        if (size > body_limit(self.path) and relay.context_recovery
                                and self.path in {'/v1/chat/completions', '/v1/messages', '/v1/responses'}):
                            # Full uncovered history can reach the transport
                            # ceiling before the provider's token check runs.
                            relay.context_required = {'input_tokens': size, 'input_budget': body_limit(self.path)}
                            return self.error(400, 'Saved context exceeds transport capacity. Compact before retrying.',
                                              'context_length_exceeded')
                        return self.error(413, 'Broker request is too large.')
                    raw = self.rfile.read(size) if self.command == 'POST' else b''
                    route,method = self.path,self.command
                    if route in MODEL_ROUTES:
                        if relay.model_failed:
                            return self.error(409, 'The failed model request is saved. Waiting for durable recovery.', 'broker_recovery_required')
                        if relay.context_recovery and relay.context_required:
                            return self.error(400, 'Context length exceeded; waiting for the saved-context handoff.', 'context_length_exceeded')
                        if relay.before_model and not relay.before_model(raw):
                            return self.error(409, 'Saving at a complete tool boundary.')
                    if credential_route:
                        try:
                            raw = json.dumps({'request_id':credential_route[1],'method':method,'path':credential_route[2],
                                              'body':json.loads(raw) if raw else {}}).encode()
                        except (ValueError,UnicodeDecodeError):
                            return self.error(422,'Invalid provider request JSON.')
                        route,method = '/credentials/invoke','POST'
                    control_call = self.path == '/credentials/materialize'
                    if self.path == '/tools/call':
                        try:
                            control_call = json.loads(raw).get('name') in {'agents_fanout', 'agents_retry', 'credentials_request', 'credentials_report_failure', 'credentials_http_request'}
                        except (ValueError, AttributeError):
                            pass
                    if self.command == 'GET' and self.path in {'/tools', '/v1/models'}:
                        if self.path == '/tools':
                            relay.startup_failure = None
                        request_id = uuid4().hex
                        request = urllib.request.Request(remote.rstrip('/') + self.path,
                            headers={'Authorization': 'Bearer ' + token, 'X-Moyai-Request-ID': request_id}, method='GET')
                        try:
                            status, content_type, body = read_with_reconnect(request,
                                lambda response: (response.status, response.headers.get('Content-Type', 'application/json'),
                                                  response.read(MAX_BODY + 1)),
                                stage='workspace_tools', notify=notify)
                        except StartupUnavailable as exc:
                            if self.path == '/tools':
                                relay.startup_failure = exc
                            return self.error(503, str(exc))
                        if self.path == '/tools':
                            relay.startup_failure = None
                        if len(body) > MAX_BODY:
                            return self.error(502, 'Workspace service reply exceeds size limit.')
                        response_status, response_bytes = status, len(body)
                        self.send_response(status)
                        self.send_header('Content-Type', content_type)
                        response_started = True
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    steering = relay.steering if self.path == '/v1/chat/completions' else None
                    with steering.model_wait() if steering else nullcontext() as generation:
                        if steering and hasattr(steering, 'receipts'):
                            value = json.loads(raw)
                            value['steering_applied'] = steering.receipts()
                            raw = json.dumps(value).encode()
                        while True:
                            if steering and (steering.cancelled(generation) if hasattr(steering, 'cancelled') else steering.requested):
                                return self.error(409, 'This model request was superseded by a queued message.')
                            data = seal(token, route, raw) if method == 'POST' else None
                            request_id = uuid4().hex
                            request_started = time.monotonic()
                            request = urllib.request.Request(remote.rstrip('/') + route, data=data,
                                headers={'Authorization': 'Bearer ' + token, 'Content-Type': CONTENT_TYPE,
                                         'X-Moyai-Request-ID': request_id,
                                         **{k: self.headers[k] for k in ('anthropic-version', 'anthropic-beta') if k in self.headers}}, method=method)
                            try:
                                response = urllib.request.urlopen(request, timeout=940)
                                break
                            except urllib.error.HTTPError as exc:
                                if (route not in {'/v1/chat/completions', '/v1/messages', '/v1/responses'} or exc.code != 429
                                        or exc.headers.get('X-Moyai-Model-Queue') != '1'):
                                    raise
                                exc.close()
                                # Never retry a submitted inference or uncertain
                                # network failure here. This marker is only emitted
                                # before admission, so no gateway call was made.
                                time.sleep(3 + random.random())
                    with response:

                        if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                            return  # A redirected request may complete/bill later; discard its output.
                        response_headers, response_status = response.headers, response.status
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        response_started = True
                        self.end_headers()
                        if control_call:
                            body = response.read(MAX_BODY + 1)
                            response_bytes += len(body)
                            if len(body) > MAX_BODY:
                                raise ValueError('Delegation reply exceeds size limit')
                            value = json.loads(body)
                            group = value.get('moyai_wait_group')
                            if isinstance(group, str) and len(group) == 32 and all(c in '0123456789abcdef' for c in group):
                                relay.wait_group = group
                            credential = value.get('moyai_wait_credential')
                            if isinstance(credential,str) and re.fullmatch(r'[0-9a-f]{32}',credential):
                                relay.wait_credential = credential
                            self.wfile.write(body)
                        else:
                            while chunk := response.read1(65536):
                                response_bytes += len(chunk)
                                if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                                    return
                                self.wfile.write(chunk)
                        # Sized reads can return EOF without raising when a
                        # declared Content-Length was only partly received.
                        if response.length:
                            raise http.client.IncompleteRead(b'', response.length)
                except urllib.error.HTTPError as exc:
                    if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                        exc.close()
                        return
                    message = EDGE_ERROR if exc.code == 403 and 'json' not in exc.headers.get('Content-Type', '') else ''
                    if not message:
                        try:
                            body = exc.read(8192)
                            response_bytes += len(body)
                            value = json.loads(body)
                            pressure = value.get('detail')
                            if (exc.headers.get('X-Moyai-Context') == 'compact' and isinstance(pressure, dict)
                                    and pressure.get('code') == 'context_compaction_required'
                                    and all(type(pressure.get(key)) is int and pressure[key] > 0
                                            for key in ('input_tokens', 'input_budget'))):
                                exc.close()
                                if relay.context_recovery:
                                    relay.context_required = pressure
                                # Hermes owns native overflow recovery. Give it
                                # the standard structured error, not a generic 502.
                                return self.error(400, 'Context length exceeded. Compact history before the next model call.', 'context_length_exceeded')
                            credential = value.get('moyai_wait_credential') if credential_route else None
                            if isinstance(credential, str) and re.fullmatch(r'[0-9a-f]{32}', credential):
                                relay.wait_credential = credential
                            message = value.get('detail') or value.get('error', {}).get('message')
                        except (ValueError, AttributeError):
                            pass
                        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as read_error:
                            response_bytes += len(getattr(read_error, 'partial', b''))
                    message = str(message or 'The cloud connection failed before a response could finish.')
                    if not credential_route:
                        relay.last_error = message
                    record_failure(exc, headers=exc.headers, status=exc.code)
                    exc.close()
                    self.error(502 if exc.code == 403 else exc.code, message)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    if not response_started:
                        self.error(422, 'Unsupported or invalid model wire payload.')
                    else:
                        relay.last_error = 'The cloud reply could not be decoded. Your message is saved.'
                        record_failure(exc, headers=response_headers, status=response_status)
                        self.close_connection = True
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                    if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                        return
                    message = 'The cloud connection timed out or could not be reached. Your message is saved.'
                    response_bytes += len(getattr(exc, 'partial', b''))
                    if not credential_route:
                        relay.last_error = message
                    record_failure(exc, headers=response_headers, status=response_status)
                    if not response_started:
                        self.error(502, message)
                    else:
                        self.close_connection = True

            do_GET = do_POST = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()
        return self

    def control(self, body=None):
        request = urllib.request.Request(self.remote.rstrip('/') + '/control',
            data=seal(self.token, '/control', json.dumps(body or {}).encode()),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                value = json.load(response)
                return value if isinstance(value, dict) else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError):
            # Control-plane failure cannot authorize interrupting or replaying work.
            return {}

    def context_window(self):
        request = urllib.request.Request(self.remote.rstrip('/') + '/context/window',
            headers={'Authorization': 'Bearer ' + self.token, 'X-Moyai-Request-ID': uuid4().hex})
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read(8192))
        if type(value.get('input_budget')) is not int or value['input_budget'] < 1:
            raise ValueError('The broker did not return a usable compaction window.')
        return value

    def native(self, body):
        route = '/context/native'
        request = urllib.request.Request(self.remote.rstrip('/') + route,
            data=seal(self.token, route, json.dumps(body).encode()),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(2_001_025)
            if len(raw) > 2_001_024:
                raise ValueError('Native state response exceeded its limit.')
            return json.loads(raw)

    def maintain(self, snapshot, ack=''):
        route = '/context/maintenance'
        request = urllib.request.Request(self.remote.rstrip('/') + route,
            data=seal(self.token, route, json.dumps({'snapshot': snapshot, 'ack': ack}).encode()),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        # This waits for durable job admission, never for summary inference.
        with urllib.request.urlopen(request, timeout=2) as response:
            raw = response.read(64_001)
            if len(raw) > 64_000:
                raise ValueError('Context maintenance response exceeded its limit.')
            return json.loads(raw)

    def compact(self, summary, entries, *, summary_bytes=None):
        """Tool-free maintenance outside the runtime's next-model checkpoint hook.

        Use the same run capability and encrypted transport. The server can
        recover rejected summaries without restarting the SDK or executing tools.
        """
        route = '/context/compact'
        body = json.dumps({'summary': summary, 'entries': entries, 'cursor_protocol': 1,
                           **({'summary_bytes': summary_bytes} if summary_bytes is not None else {})}).encode()
        # Allow the server's three accounted, tool-free recovery attempts. Queue
        # rejection happens before inference and can safely wait for admission.
        deadline = time.monotonic() + 940
        while True:
            request = urllib.request.Request(self.remote.rstrip('/') + route,
                data=seal(self.token, route, body),
                headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE,
                         'X-Moyai-Request-ID': uuid4().hex}, method='POST')
            try:
                response = urllib.request.urlopen(request, timeout=max(1, deadline - time.monotonic()))
                break
            except urllib.error.HTTPError as exc:
                if (exc.code != 429 or exc.headers.get('X-Moyai-Model-Queue') != '1'
                        or time.monotonic() + 3 >= deadline):
                    raise
                exc.close()
                time.sleep(3)
        with response:
            raw = response.read(64_001)
            if len(raw) > 64_000:
                raise ValueError('Context summary response exceeded the limit.')
            value = json.loads(raw)
            return value if 'through_seq' in value else value['summary']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
