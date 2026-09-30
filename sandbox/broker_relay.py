"""Loopback OpenAI/MCP adapter; never forwards a model-chosen destination."""
import hmac
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .broker_transport import CONTENT_TYPE, MAX_BODY, seal
except ImportError:  # Loaded by the sandbox script, outside a Python package.
    from broker_transport import CONTENT_TYPE, MAX_BODY, seal

EDGE_ERROR = ('Moyai could not reach the model because the cloud connection rejected the request. '
              'Your conversation and files are saved. An administrator needs to repair the connection; '
              'resending the same message will not fix it.')


class BrokerRelay:
    def __init__(self, remote, token):
        self.last_error = ''
        self.wait_group = ''
        self.wait_credential = ''
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
                credential_route = re.fullmatch(r'/credentials/([0-9a-f]{32})/v1(/models|/chat/completions|/completions|/embeddings|/messages)',self.path)
                authorized = hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)
                if credential_route:
                    authorized = authorized or hmac.compare_digest(self.headers.get('x-api-key',''),token)
                if not authorized:
                    return self.error(401, 'Invalid cloud session capability.')
                allowed = {'GET': {'/v1/models', '/tools'}, 'POST': {'/v1/chat/completions', '/tools/call'}}
                if not credential_route and self.path not in allowed.get(self.command, set()):
                    return self.error(404, 'Unknown broker route.')
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if size < 0 or size > MAX_BODY:
                        return self.error(413, 'Broker request is too large.')
                    raw = self.rfile.read(size) if self.command == 'POST' else b''
                    route,method = self.path,self.command
                    if credential_route:
                        try:
                            raw = json.dumps({'request_id':credential_route[1],'method':method,'path':credential_route[2],
                                              'body':json.loads(raw) if raw else {}}).encode()
                        except (ValueError,UnicodeDecodeError):
                            return self.error(422,'Invalid provider request JSON.')
                        route,method = '/credentials/invoke','POST'
                    control_call = False
                    if self.path == '/tools/call':
                        try:
                            control_call = json.loads(raw).get('name') in {'agents_fanout', 'agents_retry', 'credentials_request'}
                        except (ValueError, AttributeError):
                            pass
                    while True:
                        data = seal(token, route, raw) if method == 'POST' else None
                        request = urllib.request.Request(remote.rstrip('/') + route, data=data,
                            headers={'Authorization': 'Bearer ' + token, 'Content-Type': CONTENT_TYPE}, method=method)
                        try:
                            response = urllib.request.urlopen(request, timeout=940)
                            break
                        except urllib.error.HTTPError as exc:
                            if (self.path != '/v1/chat/completions' or exc.code != 429
                                    or exc.headers.get('X-Moyai-Model-Queue') != '1'):
                                raise
                            exc.close()
                            # Never retry a submitted inference or uncertain
                            # network failure here. This marker is only emitted
                            # before admission, so no gateway call was made.
                            time.sleep(3 + random.random())
                    with response:
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
                            while chunk := response.read(65536):
                                self.wfile.write(chunk)
                        if not credential_route:
                            relay.last_error = ''
                except urllib.error.HTTPError as exc:
                    message = EDGE_ERROR if exc.code == 403 and 'json' not in exc.headers.get('Content-Type', '') else ''
                    if not message:
                        try:
                            value = json.loads(exc.read(8192))
                            message = value.get('detail') or value.get('error', {}).get('message')
                        except (ValueError, AttributeError):
                            pass
                    message = str(message or 'The cloud connection failed before a response could finish.')
                    if not credential_route:
                        relay.last_error = message
                    self.error(502 if exc.code == 403 else exc.code, message)
                except (urllib.error.URLError, TimeoutError):
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

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
