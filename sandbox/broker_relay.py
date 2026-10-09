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
from collections import Counter

try:
    from .broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from .access_transport import broker_headers, open_broker
    from .broker_failure import MODEL_ROUTES, TRANSIENT_STATUSES, failure
    from .transport_recovery import retryable_failure
    from .startup import StartupUnavailable, read_with_reconnect, _read_with_reconnect, REPOSITORY_METADATA_BUDGET
except ImportError:  # Loaded by the sandbox script, outside a Python package.
    from broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from access_transport import broker_headers, open_broker
    from broker_failure import MODEL_ROUTES, TRANSIENT_STATUSES, failure
    from transport_recovery import retryable_failure
    from startup import StartupUnavailable, read_with_reconnect, _read_with_reconnect, REPOSITORY_METADATA_BUDGET

EDGE_ERROR = ('Moyai could not reach the model because the cloud connection rejected the request. '
              'Your conversation and files are saved. An administrator needs to repair the connection; '
              'resending the same message will not fix it.')


class InputPending(Exception):
    """This model request reached an adapter-owned live input boundary."""


class BrokerRelay:
    def __init__(self, remote, token, notify=None, report_error=None):
        self.notify = notify
        self.last_error = ''
        self.last_failure = None
        self.model_response = None
        self.report_error = report_error
        self.uncertain_tool = False
        self.retry_safe_tools = frozenset()
        self.model_failed = False
        self.failure_lock = threading.Lock()
        self.wait_group = ''
        self.wait_credential = ''
        self.remote, self.token = remote, token
        self.startup_failure = None
        self.repository_startup = False
        self.steering = None
        self.before_model = None
        self.on_context_ready = None
        self.context_required = None
        self.context_recovery = False
        self.native_compacting = False
        self.live_compaction = False
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

            def context_error(self, message):
                if self.path != '/v1/responses' or not relay.native_compacting:
                    return self.error(400, message, 'context_length_exceeded')
                # The pinned Codex compactor trims and retries this SSE error;
                # HTTP 400 maps to InvalidRequest and aborts its native recovery.
                body = ('data: ' + json.dumps({'type': 'response.failed', 'response': {
                    'error': {'code': 'context_length_exceeded', 'message': message}}}) + '\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                try:
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionError, http.client.HTTPException):
                    # This local overflow signal can outlive the SDK request.
                    # A disconnected reader is not an upstream model failure.
                    self.close_connection = True

            def handle_request(self):
                steering, generation = None, None
                route, request_id = '', ''
                response_started, response_bytes = False, 0
                response_headers, response_status = {}, None
                request_started = time.monotonic()
                tool_read = False

                def record_failure(exc, *, headers=None, status=None, message=None):
                    diagnostic = failure(route, request_id, exc, headers=headers,
                        status=status, response_started=response_started, response_bytes=response_bytes)
                    diagnostic.update(occurred_at=time.time(), duration_ms=round((time.monotonic() - request_started) * 1000))
                    with relay.failure_lock:
                        if tool_read:
                            # A confirmed safe read cannot create uncertainty or
                            # overwrite a concurrent model/write failure.
                            diagnostic['uncertain_tool'] = False
                        else:
                            if message is not None:
                                relay.last_error = message
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
                # MCP Git reads use this relay just like other tool calls. The
                # destination and repository authorization stay at the broker;
                # no push, arbitrary URL, path traversal or extra query is allowed.
                git_route = bool(re.fullmatch(r'/github/repositories/[1-9][0-9]*\.git/' +
                    (r'info/refs\?service=git-upload-pack' if self.command == 'GET' else
                     r'git-upload-pack' if self.command == 'POST' else r'(?!)'), self.path))
                tool_read = git_route
                allowed = {'GET': {'/v1/models', '/tools'}, 'POST': {'/v1/chat/completions', '/v1/messages', '/v1/responses', '/tools/call', '/credentials/materialize'}}
                if not git_route and not credential_route and self.path not in allowed.get(self.command, set()):
                    return self.error(404, 'Unknown broker route.')
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    limit = 1024 * 1024 if git_route else body_limit(self.path)
                    if size < 0 or size > limit:
                        if (size > body_limit(self.path) and relay.context_recovery
                                and self.path in {'/v1/chat/completions', '/v1/messages', '/v1/responses'}):
                            # Full uncovered history can reach the transport
                            # ceiling before the provider's token check runs.
                            relay.context_required = {'input_tokens': size, 'input_budget': body_limit(self.path)}
                            return self.context_error('Saved context exceeds transport capacity. Compact before retrying.')
                        return self.error(413, 'Broker request is too large.')
                    if git_route:
                        if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1
                                or (self.command == 'GET' and size)
                                or (self.command == 'POST' and self.headers.get('Content-Type') != 'application/x-git-upload-pack-request')
                                or self.headers.get('Content-Encoding', 'identity') not in {'identity', 'gzip'}
                                or self.headers.get('Git-Protocol', '') not in {'', 'version=2'}):
                            return self.error(400, 'Invalid Git read request.')
                    raw = self.rfile.read(size) if self.command == 'POST' else b''
                    route,method = self.path,self.command
                    if route in MODEL_ROUTES:
                        if relay.model_failed:
                            return self.error(409, 'The failed model request is saved. Waiting for durable recovery.', 'broker_recovery_required')
                        if (relay.context_recovery and relay.context_required
                                and not (relay.native_compacting and route == '/v1/responses')):
                            return self.error(400, 'Context length exceeded; waiting for the saved-context handoff.', 'context_length_exceeded')
                        relay.model_response = None
                        try:
                            if relay.before_model and not relay.before_model(raw):
                                return self.error(409, 'Saving at a complete tool boundary.')
                        except InputPending:
                            # This request's outcome cannot be overwritten by
                            # another model call or a replacement SDK stream.
                            return self.error(400, 'A queued input is ready at this model boundary.', 'moyai_input_pending')
                    if credential_route:
                        try:
                            raw = json.dumps({'request_id':credential_route[1],'method':method,'path':credential_route[2],
                                              'body':json.loads(raw) if raw else {}}).encode()
                        except (ValueError,UnicodeDecodeError):
                            return self.error(422,'Invalid provider request JSON.')
                        route,method = '/credentials/invoke','POST'
                    control_call = self.path == '/credentials/materialize'
                    repository_read = False
                    if self.path == '/tools/call':
                        try:
                            name = json.loads(raw).get('name')
                            tool_read = isinstance(name, str) and name in relay.retry_safe_tools
                            control_call = name in {'agents_fanout', 'agents_retry', 'credentials_request', 'credentials_report_failure', 'credentials_http_request'}
                            # These broker operations only read repository/PR
                            # metadata. Git mutations happen after their reply.
                            repository_read = (relay.repository_startup and self.command == 'POST'
                                               and name in {'github_checkout', 'github_repository'})
                        except (ValueError, AttributeError):
                            pass
                    if (tool_read and not git_route) or repository_read or (self.command == 'GET' and self.path in {'/tools', '/v1/models'}):
                        if self.path == '/tools' or repository_read:
                            relay.startup_failure = None
                        request_id = uuid4().hex
                        request = urllib.request.Request(remote.rstrip('/') + self.path,
                            data=seal(token, route, raw) if repository_read or tool_read else None,
                            headers={**broker_headers(remote, token), 'X-Moyai-Request-ID': request_id,
                                     **({'Content-Type': CONTENT_TYPE} if repository_read or tool_read else {})}, method=self.command)
                        try:
                            read = _read_with_reconnect if repository_read or tool_read else read_with_reconnect
                            # GitHub metadata fans out to several upstream reads;
                            # preserve its former 90s allowance per lookup.
                            options = {'budget': REPOSITORY_METADATA_BUDGET, 'attempt_timeout': 90} if repository_read or tool_read else {}
                            def read_reply(response):
                                body = response.read(MAX_BODY + 1)
                                if len(body) <= MAX_BODY and response.length:
                                    raise http.client.IncompleteRead(body, response.length)
                                return response.status, response.headers.get('Content-Type', 'application/json'), body
                            status, content_type, body = read(request, read_reply,
                                stage='tool_read' if tool_read else 'repository_metadata' if repository_read else 'workspace_tools',
                                notify=notify, opener=open_broker, **options)
                        except StartupUnavailable as exc:
                            if tool_read:
                                record_failure(exc, status=503)
                                return self.error(503, 'Workspace services are temporarily unavailable. This read can be retried.')
                            if self.path == '/tools' or repository_read:
                                relay.startup_failure = exc
                            return self.error(503, str(exc))
                        if self.path == '/tools' or repository_read:
                            relay.startup_failure = None
                        if len(body) > MAX_BODY:
                            return self.error(502, 'Workspace service reply exceeds size limit.')
                        if self.path == '/tools':
                            # Only the authenticated broker's explicit declarations
                            # authorize replay. readOnlyHint alone can include
                            # context-selection effects; caller annotations do not count.
                            try:
                                catalog = json.loads(body)
                                names = Counter(tool['name'] for tool in catalog)
                                relay.retry_safe_tools = frozenset(tool['name'] for tool in catalog
                                    if isinstance(tool['name'], str) and names[tool['name']] == 1
                                    and tool.get('annotations', {}).get('readOnlyHint') is True
                                    and tool.get('annotations', {}).get('idempotentHint') is True)
                            except (ValueError, KeyError, TypeError, AttributeError):
                                relay.retry_safe_tools = frozenset()
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
                            data = (raw if git_route else seal(token, route, raw)) if method == 'POST' else None
                            request_id = uuid4().hex
                            request_started = time.monotonic()
                            request = urllib.request.Request(remote.rstrip('/') + route, data=data,
                                headers={**broker_headers(remote, token), 'Content-Type': 'application/x-git-upload-pack-request' if git_route else CONTENT_TYPE,
                                         'X-Moyai-Request-ID': request_id,
                                         **({k: self.headers[k] for k in ('Git-Protocol', 'Content-Encoding') if k in self.headers} if git_route else {}),
                                         **{k: self.headers[k] for k in ('anthropic-version', 'anthropic-beta') if k in self.headers}}, method=method)
                            try:
                                response = open_broker(request, timeout=940)
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
                        if route == '/v1/responses':
                            # Keep only transport metadata. The native consumer
                            # identifies a clean EOF missing response.completed.
                            relay.model_response = failure(route, request_id, http.client.IncompleteRead(b''),
                                headers=response_headers, status=response_status, response_started=True)
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        if git_route and response.headers.get('Content-Length') is not None:
                            self.send_header('Content-Length', response.headers['Content-Length'])
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
                                return self.context_error('Context length exceeded. Compact history before the next model call.')
                            credential = value.get('moyai_wait_credential') if credential_route else None
                            if isinstance(credential, str) and re.fullmatch(r'[0-9a-f]{32}', credential):
                                relay.wait_credential = credential
                            message = value.get('detail') or value.get('error', {}).get('message')
                        except (ValueError, AttributeError):
                            pass
                        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as read_error:
                            response_bytes += len(getattr(read_error, 'partial', b''))
                    message = str(message or 'The cloud connection failed before a response could finish.')
                    record_failure(exc, headers=exc.headers, status=exc.code,
                                   message=message if not credential_route else None)
                    exc.close()
                    self.error(502 if exc.code == 403 else exc.code, message)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    if not response_started:
                        self.error(422, 'Unsupported or invalid model wire payload.')
                    else:
                        record_failure(exc, headers=response_headers, status=response_status,
                                       message='The cloud reply could not be decoded. Your message is saved.')
                        self.close_connection = True
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                    if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                        return
                    message = 'The cloud connection timed out or could not be reached. Your message is saved.'
                    response_bytes += len(getattr(exc, 'partial', b''))
                    record_failure(exc, headers=response_headers, status=response_status,
                                   message=message if not credential_route else None)
                    if not response_started:
                        self.error(502, message)
                    else:
                        self.close_connection = True

            do_GET = do_POST = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def note_stream_disconnect(self):
        """Correlate the native protocol EOF with its last Responses request."""
        with self.failure_lock:
            if self.last_failure is not None or self.uncertain_tool or self.model_response is None:
                return
            self.last_failure = self.model_response
            self.model_failed = True
            self.last_error = 'The model connection closed before the response completed.'
            diagnostic = self.last_failure
        if self.report_error:
            self.report_error(diagnostic)

    def model_ready(self, *, timeout):
        """One bounded authenticated read; the live runtime owns retry/cancel."""
        request = urllib.request.Request(self.remote.rstrip('/') + '/v1/models',
            headers={**broker_headers(self.remote, self.token), 'X-Moyai-Request-ID': uuid4().hex})
        try:
            with open_broker(request, timeout=timeout) as response:
                raw = response.read(8193)
                if len(raw) > 8192:
                    raise ValueError('Invalid broker readiness response')
                value = json.loads(raw)
                if (not isinstance(value, dict) or value.get('object') != 'list'
                        or not isinstance(value.get('data'), list)):
                    raise ValueError('Invalid broker readiness response')
                return True
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in TRANSIENT_STATUSES:
                raise
            return False
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            return False

    def resume_model(self, observed_failure, *, live=False):
        """Reopen only the failed model boundary a live SDK has finished handling."""
        with self.failure_lock:
            if (self.last_failure is not observed_failure or not self.model_failed
                    or self.uncertain_tool or not retryable_failure(observed_failure, live=live)):
                return False
            self.last_failure = None
            self.last_error = ''
            self.model_failed = False
            return True

    def start(self):
        self.thread.start()
        return self

    def control(self, body=None):
        request = urllib.request.Request(self.remote.rstrip('/') + '/control',
            data=seal(self.token, '/control', json.dumps(body or {}).encode()),
            headers={**broker_headers(self.remote, self.token), 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        try:
            with open_broker(request, timeout=5) as response:
                value = json.load(response)
                return value if isinstance(value, dict) else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError):
            # Control-plane failure cannot authorize interrupting or replaying work.
            return {}

    def context_window(self):
        request = urllib.request.Request(self.remote.rstrip('/') + '/context/window',
            headers={**broker_headers(self.remote, self.token), 'X-Moyai-Request-ID': uuid4().hex})
        value = read_with_reconnect(request, lambda response: json.loads(response.read(8192)),
                                    stage='context_window', notify=self.notify, opener=open_broker)
        if type(value.get('input_budget')) is not int or value['input_budget'] < 1:
            raise ValueError('The broker did not return a usable compaction window.')
        self.live_compaction = value.get('live_compaction') is True
        return value

    def native(self, body):
        route = '/context/native'
        request = urllib.request.Request(self.remote.rstrip('/') + route,
            data=seal(self.token, route, json.dumps(body).encode()),
            headers={**broker_headers(self.remote, self.token), 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        with open_broker(request, timeout=3) as response:
            raw = response.read(2_001_025)
            if len(raw) > 2_001_024:
                raise ValueError('Native state response exceeded its limit.')
            return json.loads(raw)

    def maintain(self, snapshot, ack=''):
        route = '/context/maintenance'
        request = urllib.request.Request(self.remote.rstrip('/') + route,
            data=seal(self.token, route, json.dumps({'snapshot': snapshot, 'ack': ack}).encode()),
            headers={**broker_headers(self.remote, self.token), 'Content-Type': CONTENT_TYPE,
                     'X-Moyai-Request-ID': uuid4().hex}, method='POST')
        # This waits for durable job admission, never for summary inference.
        with open_broker(request, timeout=2) as response:
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
                headers={**broker_headers(self.remote, self.token), 'Content-Type': CONTENT_TYPE,
                         'X-Moyai-Request-ID': uuid4().hex}, method='POST')
            try:
                response = open_broker(request, timeout=max(1, deadline - time.monotonic()))
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
