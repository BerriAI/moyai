import asyncio
import datetime
import os
from pathlib import Path
import sys
import hashlib
import ipaddress
import json
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sandbox.access_transport import broker_headers, git_environment, open_broker
from sandbox.attachments import prepare_attachments
from sandbox.broker_relay import BrokerRelay


@pytest.fixture
def edge_backend():
    return {}


@pytest.fixture
def access_origin(tmp_path, monkeypatch, edge_backend):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    client_context = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setenv('GIT_SSL_CAINFO', str(cert_path))
    monkeypatch.setattr(ssl, '_create_default_https_context', lambda **kwargs: client_context)
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_request(self):
            calls.append((self.path, dict(self.headers)))
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if '/redirect' in self.path:
                self.send_response(302)
                self.send_header('Location', '/stolen')
                self.end_headers()
                return
            if not edge_backend.get('access_disabled') and self.headers.get('User-Agent') != 'Moyai/1.0':
                self.send_response(403)
                self.end_headers()
                return
            if (self.headers.get('Authorization') != 'Bearer test-run'
                    or (not edge_backend.get('access_disabled') and (self.headers.get('CF-Access-Client-Id') != 'test-client'
                        or self.headers.get('CF-Access-Client-Secret') != 'test-edge-secret'))):
                self.send_response(401)
                self.end_headers()
                return
            if 'handle' in edge_backend:
                return edge_backend['handle'](self, raw)
            payload = (b'attachment bytes' if '/attachments/' in self.path else
                       json.dumps({'input_budget': 1000, 'summary': 'saved', 'object': 'list', 'data': [], 'ok': True}).encode())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        do_GET = do_POST = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'https://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('WORKSPACE_ACCESS_ORIGIN', origin)
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_ID', 'test-client')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_SECRET', 'test-edge-secret')
    try:
        yield origin + '/broker/test-run-id', calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_actual_https_relay_carries_both_auth_layers_on_every_transport(access_origin, tmp_path):
    remote, calls = access_origin
    relay = BrokerRelay(remote, 'test-run').start()
    try:
        with httpx.Client(base_url=relay.url, headers={'Authorization': 'Bearer test-run'}) as client:
            assert client.get('/tools').status_code == 200
            assert client.get('/v1/models').status_code == 200
            for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses', '/tools/call', '/credentials/materialize'):
                assert client.post(path, json={}).status_code == 200
            relay.repository_startup = True
            assert client.post('/tools/call', json={'name': 'github_checkout'}).status_code == 200
            relay.retry_safe_tools = frozenset({'memory_search'})
            assert client.post('/tools/call', json={'name': 'memory_search'}).status_code == 200
        assert relay.control()['ok']
        assert relay.model_ready(timeout=3)
        assert relay.context_window()['input_budget'] == 1000
        assert relay.native({})['ok']
        assert relay.maintain({})['ok']
        assert relay.compact('', []) == 'saved'
        content = b'attachment bytes'
        spec = {'broker_url': remote, 'attachments': [{'id': 'a' * 32, 'name': 'note.txt',
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]}
        prepare_attachments(spec, 'test-run', root=tmp_path / 'uploads')
        assert (tmp_path / 'uploads' / ('a' * 32) / 'note.txt').read_bytes() == content
        assert len(calls) == 16
    finally:
        relay.close()


def test_edge_credentials_never_follow_redirects(access_origin):
    remote, calls = access_origin
    request = urllib.request.Request(remote + '/redirect', headers=broker_headers(remote, 'test-run'))
    with pytest.raises(urllib.error.HTTPError) as error:
        open_broker(request, timeout=3)
    assert error.value.code == 302
    assert [path for path, _ in calls] == ['/broker/test-run-id/redirect']


def test_real_git_sends_both_credentials_but_refuses_redirects(access_origin):
    remote, calls = access_origin
    result = subprocess.run(['git', 'ls-remote', remote + '/redirect'],
                            env=git_environment(remote, 'test-run'), capture_output=True, timeout=10)
    assert result.returncode != 0
    assert len(calls) == 1
    path, headers = calls[0]
    assert path.startswith('/broker/test-run-id/redirect/info/refs')
    assert headers['Authorization'] == 'Bearer test-run'
    assert headers['CF-Access-Client-Secret'] == 'test-edge-secret'


@pytest.mark.parametrize('url', ['https://attacker.example/broker/run', 'http://workspace.example/broker/run',
                                  'https://workspace.example/api/credentials',
                                  'https://workspace.example.attacker.example/broker/run',
                                  'https://user@workspace.example/broker/run'])
def test_credentials_are_never_added_to_other_destinations(monkeypatch, url):
    monkeypatch.setenv('WORKSPACE_ACCESS_ORIGIN', 'https://workspace.example')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_ID', 'test-client')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_SECRET', 'test-secret')
    with pytest.raises(ValueError):
        broker_headers(url, 'test-run')


def test_git_headers_are_scoped_to_broker_url_and_do_not_persist_in_config(monkeypatch):
    remote = 'https://workspace.example/broker/run'
    monkeypatch.setenv('WORKSPACE_ACCESS_ORIGIN', 'https://workspace.example')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_ID', 'test-client')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_SECRET', 'test-secret')
    env = git_environment(remote, 'test-run')
    assert env['GIT_CONFIG_COUNT'] == '5'
    for index in range(4):
        assert env[f'GIT_CONFIG_KEY_{index}'] == 'http.' + remote + '/.extraHeader'
    assert env['GIT_CONFIG_VALUE_0'] == 'Authorization: Bearer test-run'
    assert env['GIT_CONFIG_VALUE_1'] == 'User-Agent: Moyai/1.0'
    assert env['GIT_CONFIG_VALUE_3'] == 'CF-Access-Client-Secret: test-secret'
    assert env['GIT_CONFIG_KEY_4'] == 'http.followRedirects'
    assert env['GIT_CONFIG_VALUE_4'] == 'false'


def test_public_activity_and_archives_redact_edge_credentials(monkeypatch, tmp_path):
    import zipfile
    from sandbox.activity import public_text
    from sandbox.artifacts import collect_archive
    from sandbox.trace_content import trace_content
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_ID', 'test-sensitive-client')
    monkeypatch.setenv('WORKSPACE_ACCESS_CLIENT_SECRET', 'test-sensitive-secret')
    text = 'test-sensitive-client test-sensitive-secret'
    assert 'test-sensitive' not in public_text(text)
    assert 'test-sensitive' not in trace_content({'output': text})
    workspace, artifacts = tmp_path / 'workspace', tmp_path / 'artifacts'
    workspace.mkdir(); artifacts.mkdir()
    (workspace / 'note.txt').write_text(text)
    collect_archive(workspace, artifacts, b'run-secret')
    with zipfile.ZipFile(artifacts / 'result.zip') as archive:
        assert all(b'test-sensitive' not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize(('inherit_environment', 'access_enabled'), [(False, True), (True, True), (False, False)])
def test_real_mcp_git_checkout_and_publication_through_access(
        access_origin, edge_backend, tmp_path, monkeypatch, inherit_environment, access_enabled):
    from sandbox.agent import hermes_config
    from sandbox.broker_transport import unseal
    from sandbox.harness_tools import tools_for

    remote, requests = access_origin
    if not access_enabled:
        edge_backend['access_disabled'] = True
        for name in ('WORKSPACE_ACCESS_ORIGIN', 'WORKSPACE_ACCESS_CLIENT_ID', 'WORKSPACE_ACCESS_CLIENT_SECRET'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'test-run')
    source, upstream, workspace = (tmp_path / name for name in ('source', 'upstream', 'workspace'))
    for directory in (source, upstream, workspace):
        directory.mkdir()

    def git(directory, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', *args], cwd=directory, stderr=subprocess.DEVNULL).decode().strip()

    git(source, 'init', '-b', 'main')
    (source / 'change.txt').write_text('before\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'Initial')
    bare = upstream / 'github/repositories/101.git'
    bare.parent.mkdir(parents=True)
    git(upstream, 'clone', '--bare', str(source), str(bare))
    metadata = {'repository_id': 101, 'repository': 'BerriAI/moyai', 'default_branch': 'main',
                'base_sha': git(source, 'rev-parse', 'HEAD'), 'git_path': '/github/repositories/101.git'}
    publications = []

    def handle(handler, raw):
        path, _, query = handler.path.removeprefix('/broker/test-run-id').partition('?')
        if path.startswith('/github/'):
            env = {**os.environ, 'GIT_PROJECT_ROOT': str(upstream), 'GIT_HTTP_EXPORT_ALL': '1',
                   'PATH_INFO': path, 'QUERY_STRING': query, 'REQUEST_METHOD': handler.command,
                   'CONTENT_TYPE': handler.headers.get('Content-Type', ''),
                   'HTTP_GIT_PROTOCOL': handler.headers.get('Git-Protocol', ''), 'REMOTE_USER': 'moyai',
                   'CONTENT_LENGTH': str(len(raw))}
            reply = subprocess.run(['git', 'http-backend'], env=env, input=raw,
                                   capture_output=True, check=True).stdout
            head, _, body = reply.partition(b'\r\n\r\n')
            handler.send_response(200)
            for line in head.split(b'\r\n'):
                name, _, value = line.decode().partition(':')
                if name.lower() != 'status':
                    handler.send_header(name, value.strip())
        else:
            if path == '/tools':
                value = [{'name': name, 'description': name, 'inputSchema': {'type': 'object'}}
                         for name in ('github_checkout', 'github_create_pull_request', 'github_update_pull_request')]
            else:
                assert path == '/tools/call'
                request = json.loads(unseal('test-run', path, raw))
                if request['name'] == 'github_checkout':
                    value = metadata
                else:
                    # Simulate server-side publication to a local provider. The
                    # MCP subprocess still performs a real authenticated fetch.
                    assert request['name'] in {'github_create_pull_request', 'github_update_pull_request'}
                    changes = request['arguments']['files']
                    publications.append(changes)
                    for change in changes:
                        (source / change['path']).write_text(change['content'])
                    git(source, 'add', '.')
                    git(source, 'commit', '-m', request['arguments']['title'])
                    git(source, 'push', str(bare), 'HEAD:refs/heads/moyai/test')
                    value = {'repository_id': 101, 'repository': 'BerriAI/moyai', 'number': 100,
                             'branch': 'moyai/test', 'commit': git(source, 'rev-parse', 'HEAD'),
                             'url': 'https://github.com/BerriAI/moyai/pull/100'}
            body = json.dumps(value).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    edge_backend['handle'] = handle
    relay = BrokerRelay(remote, 'test-run').start()
    try:
        config = hermes_config({'model': 'test-model', 'broker_url': remote}, relay.url, workspace)
        server = config['mcp_servers']['workspace']
        # Same shared config as every runtime, with only local executable/paths
        # substituted. Check both SDK-sanitized and inherited subprocess envs.
        bootstrap = ('import sys; from pathlib import Path; sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1]))
                     + '); from sandbox import github_tools, mcp_bridge; github_tools.ROOT=Path('
                     + repr(str(workspace)) + '); mcp_bridge.serve()')
        server.update(command=sys.executable, args=['-c', bootstrap])
        if inherit_environment:
            server['env'] = {**os.environ, **server['env']}
        call = tools_for(str(workspace), config)[1]

        def invoke(name, args):
            reply = json.loads(asyncio.run(call(name, json.dumps(args))))
            assert not reply['isError'], reply
            assert 'test-edge-secret' not in json.dumps(reply)
            return json.loads(reply['content'][0]['text'])

        result = invoke('github_checkout', {'repository_id': 101, 'directory': 'fresh'})
        repo = workspace / 'fresh'
        assert result['base_sha'] == metadata['base_sha']
        assert (repo / 'change.txt').read_text() == 'before\n'
        print('PASS real MCP subprocess: authenticated Git clone' + (' through Access gate' if access_enabled else ' without Access configured'))
        for index, name in enumerate(('github_create_pull_request', 'github_update_pull_request'), 1):
            (repo / 'change.txt').write_text(f'after {index}\n')
            args = {'directory': 'fresh', 'title': f'Change {index}', 'request_key': f'fixture-{index}'}
            args.update({'body': 'Local provider fixture'} if index == 1 else {'number': 100})
            result = invoke(name, args)
            assert 'checkout_warning' not in result
            assert json.loads((repo / '.git/moyai.json').read_text())['base_sha'] == result['commit']
        assert len(publications) == 2 and all(len(files) == 1 for files in publications)
        print('PASS publication and revision: authenticated fetch updates local checkout base')
        config_text = (repo / '.git/config').read_text()
        assert 'test-run' not in config_text and 'test-edge-secret' not in config_text
        assert 'extraHeader' not in config_text
        for _, headers in requests:
            forwarded = {key.lower(): value for key, value in headers.items()}
            assert forwarded.get('cf-access-client-secret') == ('test-edge-secret' if access_enabled else None)
        assert all(not server['env'].get(name) for name in (
            'WORKSPACE_ACCESS_ORIGIN', 'WORKSPACE_ACCESS_CLIENT_ID', 'WORKSPACE_ACCESS_CLIENT_SECRET'))
        assert not relay.uncertain_tool and relay.last_failure is None
        print('PASS no Cloudflare credentials in MCP configuration or repository config')
    finally:
        relay.close()


@pytest.mark.parametrize(('method', 'path', 'headers', 'content', 'status'), [
    ('GET', '/github/repositories/101.git/info/refs?service=git-upload-pack', {'Authorization': ''}, b'', 401),
    ('GET', '/github/repositories/101.git/info/refs?service=git-receive-pack', {}, b'', 404),
    ('POST', '/github/repositories/101.git/git-receive-pack', {}, b'', 404),
    ('GET', '/github/repositories/101.git/config', {}, b'', 404),
    ('GET', '/github/repositories/101.git/info/refs?service=git-upload-pack&extra=1', {}, b'', 404),
    ('GET', '/github/repositories/101.git/info/refs?service=git-upload-pack&service=git-upload-pack', {}, b'', 404),
    ('GET', '/github/repositories/0.git/info/refs?service=git-upload-pack', {}, b'', 404),
    ('GET', '/github/repositories/%2e%2e.git/info/refs?service=git-upload-pack', {}, b'', 404),
    ('GET', '/github/repositories/101.git/info/refs?service=git-upload-pack', {}, b'x', 400),
    ('POST', '/github/repositories/101.git/git-upload-pack', {'Content-Type': 'text/plain'}, b'x', 400),
    ('POST', '/github/repositories/101.git/git-upload-pack', {'Content-Encoding': 'br'}, b'x', 400),
    ('POST', '/github/repositories/101.git/git-upload-pack', {'Git-Protocol': 'arbitrary'}, b'x', 400),
    ('POST', '/github/repositories/101.git/git-upload-pack', {}, b'x' * (1024 * 1024 + 1), 413),
])
def test_git_relay_only_allows_authenticated_read_protocol(access_origin, method, path, headers, content, status):
    remote, calls = access_origin
    relay = BrokerRelay(remote, 'test-run').start()
    try:
        response = httpx.request(method, relay.url + path, content=content,
            headers={'Authorization': 'Bearer test-run', 'Content-Type': 'application/x-git-upload-pack-request', **headers})
        assert response.status_code == status
        assert not calls
        assert not relay.uncertain_tool
    finally:
        relay.close()


def test_git_relay_preserves_binary_protocol_and_rejects_redirects(access_origin, edge_backend):
    import gzip
    remote, calls = access_origin
    raw = gzip.compress(b'0009done\n0000\x00\xff')
    received = []
    mode = {'status': 200}
    def handle(handler, body):
        received.append((body, dict(handler.headers)))
        handler.send_response(mode['status'])
        if mode['status'] == 302:
            handler.send_header('Location', remote + '/stolen')
        handler.send_header('Content-Type', 'application/x-git-upload-pack-result')
        handler.send_header('Content-Length', '4')
        handler.end_headers()
        handler.wfile.write(b'0000')
    edge_backend['handle'] = handle
    relay = BrokerRelay(remote, 'test-run').start()
    try:
        with httpx.Client(base_url=relay.url, headers={'Authorization': 'Bearer test-run'}) as client:
            headers = {'Content-Type': 'application/x-git-upload-pack-request', 'Content-Encoding': 'gzip',
                       'Git-Protocol': 'version=2', 'CF-Access-Client-Secret': 'untrusted', 'Cookie': 'untrusted'}
            assert client.post('/github/repositories/101.git/git-upload-pack', content=raw, headers=headers).content == b'0000'
            assert received[0][0] == raw
            forwarded = {key.lower(): value for key, value in received[0][1].items()}
            assert forwarded['git-protocol'] == 'version=2' and forwarded['content-encoding'] == 'gzip'
            assert forwarded['cf-access-client-secret'] == 'test-edge-secret' and 'cookie' not in forwarded
            for status in (302, 401, 403, 503):
                mode['status'] = status
                response = client.get('/github/repositories/101.git/info/refs?service=git-upload-pack')
                assert response.status_code != 200 and 'location' not in response.headers
                assert not relay.uncertain_tool and relay.last_failure is None
        assert len(calls) == 5 and all('/stolen' not in path for path, _ in calls)
    finally:
        relay.close()
