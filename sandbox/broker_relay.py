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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import nullcontext

try:
    from .broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from .startup import StartupUnavailable, read_with_reconnect
except ImportError:  # Loaded by the sandbox script, outside a Python package.
    from broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, seal
    from startup import StartupUnavailable, read_with_reconnect

EDGE_ERROR = ('Moyai could not reach the model because the cloud connection rejected the request. '
              'Your conversation and files are saved. An administrator needs to repair the connection; '
              'resending the same message will not fix it.')


class BrokerRelay:
    def __init__(self, remote, token, notify=None):
        self.last_error = ''
        self.wait_group = ''
        self.wait_credential = ''
        self.remote, self.token = remote, token
        self.startup_failure = None
        self.steering = None
        self.before_model = None
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log capabilities, prompts or tool arguments.

            def error(self, status, message):
                content = json.dumps({'error': {'message': message, 'type': 'broker_error', 'code': 'broker_error'}}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def handle_request(self):
                steering, generation = None, None
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
                        return self.error(413, 'Broker request is too large.')
                    raw = self.rfile.read(size) if self.command == 'POST' else b''
                    route,method = self.path,self.command
                    if route in {'/v1/chat/completions', '/v1/messages', '/v1/responses'}:
                        if relay.before_model and not relay.before_model():
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
                        request = urllib.request.Request(remote.rstrip('/') + self.path,
                            headers={'Authorization': 'Bearer ' + token}, method='GET')
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
                        relay.last_error = ''
                        if len(body) > MAX_BODY:
                            return self.error(502, 'Workspace service reply exceeds size limit.')
                        self.send_response(status)
                        self.send_header('Content-Type', content_type)
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
                            request = urllib.request.Request(remote.rstrip('/') + route, data=data,
                                headers={'Authorization': 'Bearer ' + token, 'Content-Type': CONTENT_TYPE,
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
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.end_headers()
                        if control_call:
                            body = response.read(MAX_BODY + 1)
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
                                if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                                    return
                                self.wfile.write(chunk)
                        if not credential_route and not (steering and hasattr(steering, 'cancelled') and steering.cancelled(generation)):
                            relay.last_error = ''
                except urllib.error.HTTPError as exc:
                    if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                        exc.close()
                        return
                    message = EDGE_ERROR if exc.code == 403 and 'json' not in exc.headers.get('Content-Type', '') else ''
                    if not message:
                        try:
                            value = json.loads(exc.read(8192))
                            credential = value.get('moyai_wait_credential') if credential_route else None
                            if isinstance(credential, str) and re.fullmatch(r'[0-9a-f]{32}', credential):
                                relay.wait_credential = credential
                            message = value.get('detail') or value.get('error', {}).get('message')
                        except (ValueError, AttributeError):
                            pass
                    message = str(message or 'The cloud connection failed before a response could finish.')
                    if not credential_route:
                        relay.last_error = message
                    self.error(502 if exc.code == 403 else exc.code, message)
                except (ValueError, KeyError, TypeError):
                    self.error(422, 'Unsupported or invalid model wire payload.')
                except (urllib.error.URLError, TimeoutError):
                    if steering and hasattr(steering, 'cancelled') and steering.cancelled(generation):
                        return
                    message = 'The cloud connection timed out or could not be reached. Your message is saved.'
                    if not credential_route:
                        relay.last_error = message
                    self.error(502, message)

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
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                value = json.load(response)
                return value if isinstance(value, dict) else {}
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError):
            # Control-plane failure cannot authorize interrupting or replaying work.
            return {}

    def compact(self, summary, entries):
        """Tool-free maintenance outside the runtime's next-model checkpoint hook.

        Use the same run capability and encrypted transport. A failed request is
        not retried here and cannot restart the stopped SDK or execute tools.
        """
        route = '/context/compact'
        request = urllib.request.Request(self.remote.rstrip('/') + route,
            data=seal(self.token, route, json.dumps({'summary': summary, 'entries': entries}).encode()),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': CONTENT_TYPE}, method='POST')
        with urllib.request.urlopen(request, timeout=330) as response:
            raw = response.read(64_001)
            if len(raw) > 64_000:
                raise ValueError('Context summary response exceeded the limit.')
            return json.loads(raw)['summary']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
