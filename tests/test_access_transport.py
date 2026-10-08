import datetime
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
def access_origin(tmp_path, monkeypatch):
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
            if self.command == 'POST':
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if '/redirect' in self.path:
                self.send_response(302)
                self.send_header('Location', '/stolen')
                self.end_headers()
                return
            if self.headers.get('User-Agent') != 'Moyai/1.0':
                self.send_response(403)
                self.end_headers()
                return
            if (self.headers.get('CF-Access-Client-Id') != 'test-client'
                    or self.headers.get('CF-Access-Client-Secret') != 'test-edge-secret'
                    or self.headers.get('Authorization') != 'Bearer test-run'):
                self.send_response(401)
                self.end_headers()
                return
            payload = (b'attachment bytes' if '/attachments/' in self.path else
                       json.dumps({'input_budget': 1000, 'summary': 'saved', 'data': [], 'ok': True}).encode())
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
        assert relay.context_window()['input_budget'] == 1000
        assert relay.native({})['ok']
        assert relay.maintain({})['ok']
        assert relay.compact('', []) == 'saved'
        content = b'attachment bytes'
        spec = {'broker_url': remote, 'attachments': [{'id': 'a' * 32, 'name': 'note.txt',
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]}
        prepare_attachments(spec, 'test-run', root=tmp_path / 'uploads')
        assert (tmp_path / 'uploads' / ('a' * 32) / 'note.txt').read_bytes() == content
        assert len(calls) == 15
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
