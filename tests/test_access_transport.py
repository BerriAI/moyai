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
                                  'http://127.0.0.1:1234', 'http://localhost:1234',
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
    assert not any(name.startswith('WORKSPACE_ACCESS_') for name in env)
    assert os.environ['WORKSPACE_ACCESS_CLIENT_SECRET'] == 'test-secret'


def test_owned_relay_git_environment_is_bearer_only(access_origin, monkeypatch):
    from agent.tools import github_tools
    remote, _ = access_origin
    relay = BrokerRelay(remote, 'test-run').start()
    try:
        monkeypatch.setenv('WORKSPACE_BROKER_URL', relay.url)
        with pytest.raises(ValueError):
            git_environment(relay.url, 'test-run')
        env = relay.git_environment()
        assert env['GIT_CONFIG_COUNT'] == '2'
        assert env['GIT_CONFIG_KEY_0'] == 'http.' + relay.url + '/.extraHeader'
        assert env['GIT_CONFIG_VALUE_0'] == 'Authorization: Bearer test-run'
        assert env['GIT_CONFIG_KEY_1'] == 'http.followRedirects'
        assert env['GIT_CONFIG_VALUE_1'] == 'false'
        assert not any(name.startswith('WORKSPACE_ACCESS_') for name in env)
        assert 'test-edge-secret' not in json.dumps(env)
        assert os.environ['WORKSPACE_ACCESS_CLIENT_SECRET'] == 'test-edge-secret'
        observed = []
        monkeypatch.setattr(github_tools, 'checkout', lambda *args, **kwargs: observed.append((args, kwargs)))
        github_tools.call('github_checkout', {}, None, 'https://attacker.example', 'wrong-token', relay=relay)
        assert observed == [((None, relay.url, relay.token), {'env': env})]
    finally:
        relay.close()


def test_public_activity_and_archives_redact_edge_credentials(monkeypatch, tmp_path):
    import zipfile
    from agent.activity import public_text
    from sandbox.artifacts import collect_archive
    from agent.trace_content import trace_content
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


@pytest.mark.parametrize(('inherit_environment', 'access_enabled', 'remote_tool'), [
    (False, True, False), (True, True, False), (False, False, False),
    (True, True, True), (True, False, True),
])
def test_real_mcp_git_checkout_and_publication_through_access(
        access_origin, edge_backend, tmp_path, monkeypatch, inherit_environment, access_enabled, remote_tool):
    from sandbox.agent import hermes_config
    from sandbox.broker_transport import unseal
    from agent.tools.harness_tools import tools_for

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
                         for name in ('github_checkout', 'github_create_pull_request', 'github_update_pull_request',
                                      'github_publish_branch', 'github_rebase_pull_request')]
            else:
                assert path == '/tools/call'
                request = json.loads(unseal('test-run', path, raw))
                if request['name'] == 'github_checkout':
                    value = metadata
                    if request['arguments'].get('number'):
                        value = {**metadata, 'number': 100, 'checkout_ref': 'refs/heads/moyai/test',
                                 'base_sha': git(source, 'rev-parse', 'HEAD')}
                    elif request['arguments'].get('ref'):
                        ref = request['arguments']['ref']
                        assert ref in {'moyai/test', 'main'}
                        value = {**metadata, 'checkout_ref': 'refs/heads/' + ref, 'base_branch': 'main',
                                 'base_sha': metadata['base_sha'] if ref == 'main' else git(source, 'rev-parse', 'HEAD')}
                else:
                    # Simulate server-side publication to a local provider. The
                    # MCP subprocess still performs a real authenticated fetch.
                    assert request['name'] in {'github_create_pull_request', 'github_update_pull_request',
                                               'github_publish_branch', 'github_rebase_pull_request'}
                    if request['name'] == 'github_create_pull_request':
                        assert request['arguments']['base_branch'] == 'main'
                        assert request['arguments']['head_branch'] == 'moyai/test'
                    if request['name'] == 'github_rebase_pull_request':
                        assert request['arguments']['base_sha'] == metadata['base_sha']
                        assert request['arguments']['number'] == 100
                        assert set(request['arguments']) == {'repository_id', 'base_sha', 'files', 'title',
                                                              'number', 'expected_head_sha', 'request_key'}
                        git(source, 'checkout', '--detach', request['arguments']['base_sha'])
                    changes = request['arguments']['files']
                    publications.append(changes)
                    for change in changes:
                        (source / change['path']).write_text(change['content'])
                    git(source, 'add', '.')
                    git(source, 'commit', '-m', request['arguments']['title'])
                    if request['name'] == 'github_rebase_pull_request':
                        commit = git(source, 'rev-parse', 'HEAD')
                        git(bare, 'fetch', '--no-tags', str(source), commit)
                        git(bare, 'update-ref', 'refs/heads/moyai/test', commit, request['arguments']['expected_head_sha'])
                    else:
                        git(source, 'push', str(bare), 'HEAD:refs/heads/moyai/test')
                    value = {'repository_id': 101, 'repository': 'BerriAI/moyai', 'number': 100,
                             'branch': 'moyai/test', 'commit': git(source, 'rev-parse', 'HEAD'),
                             'url': 'https://github.com/BerriAI/moyai/pull/100'}
                    if request['name'] == 'github_rebase_pull_request':
                        value['base_branch'] = 'main'
                    if request['name'] == 'github_publish_branch':
                        assert request['arguments']['branch'] == 'moyai/test'
                        assert 'body' not in request['arguments'] and 'number' not in request['arguments']
                        value.pop('number')
                        value.pop('url')
                        value.update(kind='branch', base_branch='main')
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
                     + '); from agent.tools import github_tools, mcp_bridge; github_tools.ROOT=Path('
                     + repr(str(workspace)) + '); mcp_bridge.serve()')
        server.update(command=sys.executable, args=['-c', bootstrap])
        if inherit_environment:
            server['env'] = {**os.environ, **server['env']}
        call = tools_for(str(workspace), config)[1]

        def invoke(name, args):
            if remote_tool:
                program = ('import json, sys; from pathlib import Path; '
                           'from agent.tools import github_tools; '
                           'github_tools.ROOT = Path(json.loads(sys.argv[1])["workspace"]); '
                           'from sandbox.remote_tool import run; '
                           'run(json.loads(sys.argv[1]))')
                spec = {'workspace': str(workspace), 'broker_url': remote,
                        'name': name, 'arguments': args}
                execution = subprocess.run([sys.executable, '-c', program, json.dumps(spec)],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                    timeout=30, check=True, env={**os.environ, 'SSL_CERT_FILE': os.environ['GIT_SSL_CAINFO'],
                                               'MOYAI_WORKSPACE': str(workspace)})
                events = [json.loads(line.removeprefix('WORKSPACE_EVENT '))
                          for line in execution.stdout.splitlines() if line.startswith('WORKSPACE_EVENT ')]
                reply = events[-1]['tool_result']
            else:
                reply = json.loads(asyncio.run(call(name, json.dumps(args))))
            assert not reply['isError'], reply
            assert 'test-edge-secret' not in json.dumps(reply)
            return json.loads(reply['content'][0]['text'])

        result = invoke('github_checkout', {'repository_id': 101, 'directory': 'fresh'})
        repo = workspace / 'fresh'
        assert result['base_sha'] == metadata['base_sha']
        assert (repo / 'change.txt').read_text() == 'before\n'
        print('PASS real MCP subprocess: authenticated Git clone' + (' through Access gate' if access_enabled else ' without Access configured'))
        for index, name in enumerate(('github_create_pull_request', 'github_update_pull_request', 'github_publish_branch'), 1):
            (repo / 'change.txt').write_text(f'after {index}\n')
            args = {'directory': 'fresh', 'title': f'Change {index}', 'request_key': f'fixture-{index}'}
            args.update({'body': 'Local provider fixture', 'base_branch': 'main', 'head_branch': 'moyai/test'} if index == 1 else
                        {'number': 100} if index == 2 else {'branch': 'moyai/test', 'expected_head_sha': result['commit']})
            result = invoke(name, args)
            assert 'checkout_warning' not in result
            assert json.loads((repo / '.git/moyai.json').read_text())['base_sha'] == result['commit']
            if name == 'github_publish_branch':
                assert 'number' not in result and 'number' not in json.loads((repo / '.git/moyai.json').read_text())
        checked_out = invoke('github_checkout', {'repository_id': 101, 'number': 100, 'directory': 'pr-head'})
        assert checked_out['base_sha'] == result['commit']
        assert (workspace / 'pr-head/change.txt').read_text() == 'after 3\n'
        branch_checkout = invoke('github_checkout', {'repository_id': 101, 'ref': 'moyai/test', 'directory': 'branch-head'})
        assert branch_checkout['base_sha'] == result['commit'] and branch_checkout['base_branch'] == 'main'
        assert (workspace / 'branch-head/change.txt').read_text() == 'after 3\n'
        rebase_checkout = invoke('github_checkout', {'repository_id': 101, 'ref': 'main', 'directory': 'rebase-base'})
        assert rebase_checkout['base_sha'] == metadata['base_sha'] and 'number' not in rebase_checkout
        (workspace / 'rebase-base/change.txt').write_text('after rebase\n')
        rebased = invoke('github_rebase_pull_request', {'directory': 'rebase-base', 'title': 'Rebase feature',
            'number': 100, 'expected_head_sha': result['commit'], 'request_key': 'fixture-rebase'})
        assert 'checkout_warning' not in rebased and rebased['number'] == 100 and rebased['base_branch'] == 'main'
        assert rebased['branch'] == 'moyai/test' and rebased['url'] == 'https://github.com/BerriAI/moyai/pull/100'
        synchronized = json.loads((workspace / 'rebase-base/.git/moyai.json').read_text())
        assert synchronized['number'] == 100 and synchronized['base_sha'] == rebased['commit']
        assert len(publications) == 4 and all(len(files) == 1 for files in publications)
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


def test_github_packager_classification_is_shared_across_runtime_paths(monkeypatch):
    from agent.tools import github_tools, mcp_bridge, remote_computer
    from app import deepagents_runtime

    monkeypatch.delenv('MOYAI_REMOTE_COMPUTER', raising=False)
    packaged, forwarded = [], []
    names = github_tools.LOCAL_TOOLS | {'github_create_branch', 'github_open_pull_request'}
    catalog = [{'name': name} for name in names]

    def broker(path, body=None):
        if path == '/tools':
            return catalog
        forwarded.append(body['name'])
        return {'operation': body['name']}

    def local_call(name, *args, **kwargs):
        packaged.append(name)
        return {'operation': name}

    monkeypatch.setattr(mcp_bridge, 'broker', broker)
    monkeypatch.setattr(github_tools, 'call', local_call)
    assert deepagents_runtime.requires_computer is remote_computer.requires_computer
    for name in sorted(names):
        assert remote_computer.requires_computer(name) == (name in github_tools.LOCAL_TOOLS)
        result = mcp_bridge.call_tool({'name': name, 'arguments': {}}, catalog)
        assert not result['isError']
    assert set(packaged) == github_tools.LOCAL_TOOLS
    assert set(forwarded) == {'github_create_branch', 'github_open_pull_request'}
    catalog.clear()
    result = mcp_bridge.call_tool({'name': 'github_publish_branch', 'arguments': {}}, catalog)
    assert result['isError'] and len(packaged) == len(github_tools.LOCAL_TOOLS)


async def test_deepagents_combined_catalog_preserves_all_tools_with_two_wire_functions(tmp_path, monkeypatch):
    from dataclasses import asdict
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from app.config import Settings
    from app.main import create_app
    from app.deepagents_context import BrokerModel, _runtime
    from app.deepagents_runtime import DeepAgentsRuntime, SealedModelTransport
    from app.deepagents_workflow import MoyaiControl, TurnScope, workspace_call, workspace_tools
    from agent.tools import computer_tools, github_tools
    from agent.tools.mcp_bridge import BROWSER_TOOLS, GUIDANCE_TOOL
    from sandbox.broker_transport import CONTENT_TYPE, unseal
    from deepagents_provider import answer, response_stream
    from test_spend import active, sign_in

    settings = Settings(_env_file=None, data_dir=tmp_path, temporal_enabled=True,
        session_secret='test-session-secret', encryption_key=Fernet.generate_key().decode(),
        agent_model='openai/gpt-6-astra', session_titles_enabled=False, memory_review_enabled=False,
        repository_context_enabled=False, bundled_skills_enabled=False, litellm_api_key='',
        modal_token_id='', modal_token_secret='')
    app = create_app(settings)
    client = TestClient(app, base_url=settings.public_url)
    requests, wire_sizes = [], []
    runtime_token = None
    try:
        sign_in(app, client)
        connections = ['github', 'slack', 'notion', 'linear']
        for provider in connections:
            app.state.connectors.save(provider, {'access_token': 'catalog-test-token', 'kind': 'personal'}, 'Synthetic catalog')
        run = active(app)
        app.state.store.execute('UPDATE runs SET plugins=?,harness=? WHERE id=?',
                               (json.dumps(connections), settings.agent_harness, run['id']))
        scope = TurnScope(run['id'], run['active_message_id'], actor_id=run['active_user_id'], model=settings.resolve_model())
        assert settings.agent_harness == 'deepagents-temporal'
        broker_response = client.get(f"/broker/{run['id']}/tools", headers={'Authorization': 'Bearer capability'})
        assert broker_response.status_code == 200
        broker_catalog = broker_response.json()
        runtime = object.__new__(DeepAgentsRuntime)
        runtime.runner = SimpleNamespace(settings=settings, token=lambda *args: 'capability')
        runtime.current = lambda selected: app.state.store.run(selected.run_id)
        runtime.stream_text = lambda *args: None
        runtime.broker_transport = httpx.ASGITransport(app=app)
        runtime.model_request = runtime._model_request
        runtime_token = _runtime.set(runtime)

        def provider(request):
            assert request.url.path == f"/broker/{run['id']}/v1/responses"
            assert request.headers['content-type'] == CONTENT_TYPE
            raw = unseal('capability', '/v1/responses', request.read())
            body = json.loads(raw)
            requests.append(body)
            wire_sizes.append(len(raw))
            assert [tool['name'] for tool in body['tools']] == ['workspace_tools', 'workspace_call']
            assert all(tool['type'] == 'function' for tool in body['tools'])
            assert not any(tool.get('defer_loading') for tool in body['tools'])
            assert all(not {'scope', 'call_id'} & tool['parameters']['properties'].keys() for tool in body['tools'])
            response = {**answer('Catalog inspected.', identity=f'catalog-{len(requests)}'), 'model': scope.model}
            if len(requests) == 1:
                response['output'] = [{'id': 'fc_catalog', 'type': 'function_call', 'name': 'workspace_tools',
                                       'arguments': '{}', 'call_id': 'catalog-discovery', 'status': 'completed'}]
            return httpx.Response(200, content=b''.join(response_stream(response)),
                                  headers={'Content-Type': 'text/event-stream'})

        class CapturedModelTransport(SealedModelTransport):
            def __init__(self, token):
                self.token = token
                self.transport = httpx.MockTransport(provider)

        async def wait_condition(predicate):
            while not predicate():
                await asyncio.sleep(0)

        monkeypatch.setattr('app.deepagents_runtime.SealedModelTransport', CapturedModelTransport)
        monkeypatch.setattr('app.deepagents_workflow.workflow.wait_condition', wait_condition)
        control = object.__new__(MoyaiControl)
        control.scope, control.requests, control.applied = scope, 1, []
        control.inbox, control.result = [], None
        unscoped = StructuredTool.from_function(lambda: 'unused', name='unscoped_helper', description='Not model-visible.')
        offered_tools = [workspace_tools, unscoped, workspace_call]

        async def request_model(request):
            bound = request.model.bind_tools([convert_to_openai_tool(tool) for tool in request.tools])
            response = await bound.ainvoke(request.messages, **request.model_settings)
            return ModelResponse(result=[response])

        messages = [HumanMessage(content='Inspect the catalog without executing connector writes.')]
        first = await control.awrap_model_call(
            ModelRequest(model=BrokerModel(), messages=messages, tools=offered_tools), request_model)
        assert first.result[0].tool_calls[0]['name'] == 'workspace_tools'
        catalog_json = await workspace_tools.ainvoke({'scope': asdict(scope)})
        catalog = json.loads(catalog_json)
        expected = github_tools.advertised_tools(broker_catalog) + BROWSER_TOOLS + [GUIDANCE_TOOL] + computer_tools.TOOLS
        assert catalog == expected
        names = [tool['name'] for tool in catalog]
        assert len(names) == len(set(names))
        added = set('''github_create_branch github_publish_branch github_open_pull_request github_rebase_pull_request
            github_reopen_pull_request github_retarget_pull_request github_convert_pull_request_to_draft
            github_create_review github_submit_review github_edit_pending_review github_delete_pending_review
            github_stacks github_stack github_create_stack github_add_to_stack github_unstack'''.split())
        assert len(added) == 16 and added <= set(names)
        assert {'github_repositories', 'slack_search', 'notion_search', 'linear_my_issues', 'computer_operation_read'} <= set(names)
        messages += [first.result[0], ToolMessage(content=catalog_json, tool_call_id='catalog-discovery', name='workspace_tools')]
        second = await control.awrap_model_call(
            ModelRequest(model=BrokerModel(), messages=messages, tools=offered_tools), request_model)
        assert second.result[0].text == 'Catalog inspected.'
        assert len(requests) == 2 and all(body['model'] == scope.model and body['store'] is False for body in requests)
        assert not any(item.get('type') == 'function_call_output' for item in requests[0]['input'])
        output = next(item['output'] for item in requests[1]['input'] if item.get('type') == 'function_call_output')
        assert json.loads(output) == expected
        assert requests[0]['tools'] == requests[1]['tools']
        print(json.dumps({'broker_tools': len(broker_catalog), 'catalog_tools': len(catalog),
            'new_github_operations': len(added), 'bound_tools_each_request': [len(body['tools']) for body in requests],
            'catalog_utf8_bytes': len(catalog_json.encode()), 'unsealed_request_utf8_bytes': wire_sizes}))
    finally:
        if runtime_token is not None:
            _runtime.reset(runtime_token)
        client.close()
        app.state.store.close()


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
