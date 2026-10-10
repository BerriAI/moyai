import asyncio
import base64
import json
from pathlib import Path
import py_compile
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import modal
from modal._utils.name_utils import check_object_name
import pytest

from app.config import Settings
from app.computer import Command
from app.db import Store
from app.runner import RunManager
from app.sandboxes.modal import ModalProvider
from app.sandboxes.substrate import SubstrateProvider, Sandbox
from app.sandboxes.proto import ateapi_pb2 as pb
from sandbox import substrate_guest as guest
from tests.test_workspace import workspace


async def test_modal_names_preserve_existing_identity_and_bound_computer_operations(monkeypatch):
    clients = SimpleNamespace(get=AsyncMock(return_value='client'))
    settings = Settings(_env_file=None)
    create, find = AsyncMock(), AsyncMock()
    monkeypatch.setattr(modal.App, 'lookup', SimpleNamespace(aio=AsyncMock(return_value='app')))
    monkeypatch.setattr(modal.Sandbox, 'create', SimpleNamespace(aio=create))
    monkeypatch.setattr(modal.Sandbox, 'from_name', SimpleNamespace(aio=find))
    names = ['moyai-' + 'a' * 32 + '-123-0', 'a' * 64, 'a' * 65,
             'moyai-' + 'a' * 32 + '-computer-' + 'b' * 32 + '-0',
             'moyai-' + 'a' * 32 + '-computer-' + 'b' * 32 + '-1']
    identities = []
    for name in names:
        provider = ModalProvider(settings, clients)
        provider.image = lambda: 'image'
        await provider.create(name=name)
        identity = create.call_args.kwargs['name']
        check_object_name(identity, 'Sandbox')
        if len(name) <= 64:
            assert identity == name
        await ModalProvider(settings, clients).find(name)
        assert find.call_args.args == (settings.modal_app_name, identity)
        identities.append(identity)
    assert len(set(identities)) == len(names)


def key():
    value = Ed25519PrivateKey.generate()
    return value, base64.b64encode(value.private_bytes_raw()).decode()


def test_readiness_only_requires_selected_provider(tmp_path):
    _, signing = key()
    settings = Settings(_env_file=None, data_dir=tmp_path, sandbox_provider='substrate',
                        substrate_api_url='https://api.example.com', substrate_router_url='https://router.example.com',
                        substrate_api_token='api-token', substrate_signing_key=signing,
                        litellm_api_base='https://llm.example.com', litellm_api_key='llm-key', agent_model='test')
    assert settings.missing_cloud() == []
    assert settings.missing_sandbox('modal') == ['MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET']


def test_actor_template_requires_digest_and_projects_actor_identity():
    from scripts.substrate_template import template
    args = {'image': 'registry/moyai:latest', 'storage': 'gs://bucket/moyai', 'public_key': 'test'}
    with pytest.raises(ValueError, match='requires an image digest'):
        template(**args)
    args['image'] = 'registry/moyai@sha256:' + 'a' * 64
    result = template(**args)
    assert result['containers'][0]['image'] == args['image']
    assert result['volumes'][0]['systemInfo']['dataSources'][0]['actorMetadata']['items'][0]['field'] == 'ACTOR_METADATA_FIELD_UID'


@pytest.mark.parametrize('url', ['http://untrusted.example', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com?token=x'])
def test_connection_endpoint_validation(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, substrate_api_url=url)


def test_default_switch_preserves_existing_sessions(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    store = Store(tmp_path)
    manager = RunManager(store, settings)
    old = store.create_run('An old session', '', 'modal', [])
    settings.sandbox_provider = 'substrate'
    new = store.create_run('A new session', '', 'modal', [])
    assert manager.provider(old).name == 'modal'
    assert manager.provider(new).name == 'substrate'
    assert Store(tmp_path).run(old['id'])['sandbox_provider'] == 'modal'


def connection():
    return {'provider': 'substrate', 'revision': 0, 'values': {
        'substrate_api_url': 'https://api.example.com', 'substrate_router_url': 'https://router.example.com',
        'substrate_api_token': 'private-substrate-token', 'substrate_atespace': 'moyai', 'substrate_template': 'moyai'}}


def test_connect_checks_before_publishing_and_encrypts_settings(workspace, monkeypatch):
    app, client = workspace
    check = AsyncMock(return_value='Live sandbox passed')
    monkeypatch.setattr(SubstrateProvider, 'check', check)
    response = client.put('/api/settings/sandboxes', json=connection())
    assert response.status_code == 200, response.text
    assert response.json()['provider'] == 'substrate'
    assert check.call_count == 1
    assert 'private-substrate-token' not in response.text
    assert 'private-substrate-token' not in app.state.store.rows('SELECT encrypted FROM sandbox_settings')[0]['encrypted']
    assert app.state.settings.substrate_api_token == 'private-substrate-token'
    assert client.put('/api/settings/sandboxes', json=connection()).status_code == 409


def test_failed_connect_does_not_change_saved_default(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(SubstrateProvider, 'check', AsyncMock(side_effect=RuntimeError('secret-error-token')))
    result = client.put('/api/settings/sandboxes', json=connection())
    assert result.status_code == 502
    assert 'secret-error-token' not in result.text
    assert app.state.settings.sandbox_provider == 'modal'


def test_connections_require_admin_and_csrf(workspace):
    app, client = workspace
    client.headers.pop('X-CSRF-Token')
    assert client.put('/api/settings/sandboxes', json=connection()).status_code == 403
    assert 'substrate_api_token' not in app.state.sandbox_settings.view(False)['providers']['substrate']


def test_saved_connection_survives_restart(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(SubstrateProvider, 'check', AsyncMock(return_value='Connected'))
    assert client.put('/api/settings/sandboxes', json=connection()).status_code == 200
    from app.sandbox_settings import SandboxSettings
    from app.security import Security
    settings = Settings(_env_file=None, data_dir=app.state.settings.data_dir)
    security = Security(settings)
    SandboxSettings(app.state.store, settings, security)
    assert settings.sandbox_provider == 'substrate'
    assert settings.substrate_api_token == 'private-substrate-token'
    assert settings.substrate_signing_key == app.state.settings.substrate_signing_key


@pytest.fixture
def transport(tmp_path, monkeypatch):
    private, signing = key()
    identity = tmp_path / 'identity'
    identity.write_text('actor-one')
    monkeypatch.setattr(guest, 'IDENTITY', identity)
    monkeypatch.setattr(guest, 'ROOT', tmp_path / 'runtime')
    monkeypatch.setattr(guest, 'NONCES', {})
    monkeypatch.setenv('MOYAI_SUBSTRATE_PUBLIC_KEY', base64.b64encode(private.public_key().public_bytes_raw()).decode())
    server = guest.ThreadingHTTPServer(('127.0.0.1', 0), guest.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(_env_file=None, substrate_signing_key=signing,
                        substrate_router_url=f'http://127.0.0.1:{server.server_port}')
    actor = pb.Actor(metadata={'atespace': 'tests', 'name': 'one', 'uid': 'actor-one'})
    yield Sandbox(SubstrateProvider(settings), actor), tmp_path
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def test_real_http_signed_file_transfer_and_commands(transport):
    sandbox, tmp = transport
    target = str(tmp / 'test.txt')
    await sandbox.filesystem.write_text.aio('unicode ✓', target)
    assert (await sandbox.filesystem.stat.aio(target)).size == len('unicode ✓'.encode())
    assert (await sandbox.filesystem.read_bytes.aio(target)).decode() == 'unicode ✓'
    process = await sandbox.exec.aio(sys.executable, '-c', 'import sys; print("out ✓"); print("err ✓", file=sys.stderr)', timeout=10)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert out == 'out ✓\n' and err == 'err ✓\n'
    assert await process.wait.aio() == 0


async def test_real_http_streaming_drains_multiple_chunks_and_nonzero_exit(transport):
    sandbox, _ = transport
    process = await sandbox.exec.aio(sys.executable, '-c', 'import sys; print("x"*700000); print("y"*700000,file=sys.stderr); sys.exit(7)', timeout=10)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert out == 'x'*700000+'\n' and err == 'y'*700000+'\n'
    assert await process.wait.aio() == 7


async def test_chunked_unicode_writes_and_empty_overwrite(transport):
    sandbox, tmp = transport
    path = str(tmp / 'large.txt')
    value = '✓' * 800000
    await sandbox.filesystem.write_text.aio(value, path)
    assert (await sandbox.filesystem.read_bytes.aio(path)).decode() == value
    await sandbox.filesystem.write_text.aio('', path)
    assert await sandbox.filesystem.read_bytes.aio(path) == b''


@pytest.fixture
def runtime_transport(
    transport: tuple[Sandbox, Path], monkeypatch: pytest.MonkeyPatch,
) -> tuple[Sandbox, Path, Path, dict[str, str], list[tuple[str, str]]]:
    from app import runtime_files

    sandbox, tmp = transport
    source, target = tmp / 'source', tmp / 'installed'
    source.mkdir()
    target.mkdir()
    expected = {'agent.py': "VALUE = 'current ✓'\n", 'helper.py': 'UNCHANGED = True\n',
                'new.py': 'NEW = True\n', 'hermes-test.patch': 'runtime patch ✓\n'}
    for name, content in expected.items():
        (source / name).write_text(content)
        (target / name).write_text(content)
    (source / 'README.md').write_text('not a runtime file')
    (source / 'nested').mkdir()
    (source / 'nested' / 'ignored.py').write_text('not a top-level runtime file')
    (target / 'obsolete.py').write_text('preserved older runtime file')
    (target / 'user-note.txt').write_text('preserved user file')
    monkeypatch.setattr(runtime_files, 'RUNTIME_ROOT', str(target))
    monkeypatch.setattr(runtime_files, 'SANDBOX_PYTHON', sys.executable)
    writes: list[tuple[str, str]] = []
    write = sandbox.filesystem.write_text.aio

    async def capture(text: str, path: str) -> None:
        writes.append((text, path))
        await write(text, path)

    monkeypatch.setattr(sandbox.filesystem, 'write_text', SimpleNamespace(aio=capture))
    return sandbox, source, target, expected, writes


async def test_runtime_sync_skips_real_http_uploads_for_matching_files(
    runtime_transport: tuple[Sandbox, Path, Path, dict[str, str], list[tuple[str, str]]],
) -> None:
    from app.runtime_files import sync_runtime

    sandbox, source, target, expected, writes = runtime_transport
    before = {name: (target / name).stat().st_mtime_ns for name in expected}
    await sync_runtime(sandbox, source)
    assert writes == []
    assert {name: (target / name).stat().st_mtime_ns for name in expected} == before


async def test_runtime_sync_bundles_only_changed_files_without_following_symlinks(
    runtime_transport: tuple[Sandbox, Path, Path, dict[str, str], list[tuple[str, str]]],
) -> None:
    from app.runtime_files import sync_runtime

    sandbox, source, target, expected, writes = runtime_transport
    (target / 'agent.py').write_text(expected['agent.py'].replace('current', 'outdate'))
    bytecode = Path(py_compile.compile(str(target / 'agent.py'), doraise=True))
    (target / 'new.py').unlink()
    (target / 'hermes-test.patch').unlink()
    (target / 'hermes-test.patch').symlink_to(target / 'user-note.txt')
    unchanged = (target / 'helper.py').stat().st_mtime_ns
    await sync_runtime(sandbox, source)
    assert len(writes) == 1
    text, path = writes[0]
    contents = json.loads(zlib.decompress(base64.b64decode(text)))
    assert set(contents) == {'agent.py', 'new.py', 'hermes-test.patch'}
    assert path.startswith('/tmp/moyai-runtime-') and path.endswith('.bundle')
    assert {name: (target / name).read_text() for name in expected} == expected
    assert not (target / 'hermes-test.patch').is_symlink()
    assert (target / 'helper.py').stat().st_mtime_ns == unchanged
    assert (target / 'user-note.txt').read_text() == 'preserved user file'
    assert (target / 'obsolete.py').read_text() == 'preserved older runtime file'
    assert not (target / 'README.md').exists() and not (target / 'nested').exists()
    assert not bytecode.exists()
    process = await sandbox.exec.aio(sys.executable, '-I', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); import agent; print(agent.VALUE)', str(target), timeout=10)
    out, err = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    assert await process.wait.aio() == 0 and out == 'current ✓\n' and not err
    await sync_runtime(sandbox, source)
    assert len(writes) == 1


@pytest.mark.parametrize('damage', ['bundle', 'unchanged_file'])
async def test_runtime_sync_rejects_incomplete_update_and_recovers(
    runtime_transport: tuple[Sandbox, Path, Path, dict[str, str], list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    from app.runtime_files import sync_runtime

    sandbox, source, target, expected, writes = runtime_transport
    (target / 'agent.py').write_text('outdated runtime')
    (target / 'new.py').unlink()
    write = sandbox.filesystem.write_text.aio

    async def corrupt(text: str, path: str) -> None:
        if damage == 'bundle':
            contents = json.loads(zlib.decompress(base64.b64decode(text)))
            contents['new.py'] += 'corrupted in transit'
            text = base64.b64encode(zlib.compress(json.dumps(contents).encode())).decode()
        else:
            # Simulate a changed file after comparison that was absent from the bundle.
            (target / 'helper.py').write_text('changed between check and apply')
        await write(text, path)

    monkeypatch.setattr(sandbox.filesystem, 'write_text', SimpleNamespace(aio=corrupt))
    with pytest.raises(RuntimeError):
        await sync_runtime(sandbox, source)
    assert len(writes) == 1
    if damage == 'bundle':
        assert (target / 'agent.py').read_text() == 'outdated runtime'
        assert not (target / 'new.py').exists()
    monkeypatch.setattr(sandbox.filesystem, 'write_text', SimpleNamespace(aio=write))
    await sync_runtime(sandbox, source)
    assert len(writes) == 2
    assert {name: (target / name).read_text() for name in expected} == expected
    assert (target / 'user-note.txt').read_text() == 'preserved user file'
    await sync_runtime(sandbox, source)
    assert len(writes) == 2


async def test_checkpoint_blocks_other_handle_requests_until_thawed():
    actor = pb.Actor(metadata={'atespace': 'tests', 'name': 'lock-test', 'uid': 'actor-lock-test'})
    backend = SubstrateProvider(Settings(_env_file=None))
    entered, release, read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def rpc(method, data, **kwargs):
        if method == 'CreateTag':
            entered.set()
            await release.wait()
    backend.rpc = AsyncMock(side_effect=rpc)
    original, reconnected = Sandbox(backend, actor), Sandbox(backend, actor)
    original._request = AsyncMock(return_value={})
    async def read_request(*args):
        read.set()
    reconnected._request = read_request
    checkpoint = asyncio.create_task(original.snapshot_filesystem.aio())
    await entered.wait()
    reader = asyncio.create_task(reconnected.request('/read', {}))
    await asyncio.sleep(0)
    assert not read.is_set()
    release.set()
    await asyncio.gather(checkpoint, reader)
    assert read.is_set()


@pytest.mark.parametrize('path', ['/activate', '/computer'])
async def test_actor_identity_and_signing_key_are_enforced(transport, path):
    sandbox, _ = transport
    sandbox.actor.metadata.uid = 'other-actor'
    with pytest.raises(RuntimeError, match='401'):
        await sandbox.request(path, {})
    sandbox.actor.metadata.uid = 'actor-one'
    _, sandbox.provider.settings.substrate_signing_key = key()
    with pytest.raises(RuntimeError, match='401'):
        await sandbox.request(path, {})


def test_clone_activation_kills_frozen_processes_and_erases_parent_capabilities(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    (root / 'jobs').mkdir(parents=True)
    (root / 'jobs' / 'secret').write_text('parent token')
    identity = tmp_path / 'identity'
    identity.write_text('child')
    guest.atomic(root / 'frozen.json', {'uid': 'parent', 'processes': {'42': 'start'}})
    monkeypatch.setattr(guest, 'ROOT', root)
    monkeypatch.setattr(guest, 'IDENTITY', identity)
    monkeypatch.setattr(guest, 'process_identity', lambda pid: ('start', 'T'))
    calls = []
    monkeypatch.setattr(guest.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    assert guest.activate() is True
    assert calls == [(42, guest.signal.SIGKILL)]
    assert not (root / 'jobs').exists()
    assert (root / 'frozen.json').exists()
    with pytest.raises(RuntimeError, match='checkpoint'):
        guest.dispatch('/file/stat', {'path': str(identity)})
    with pytest.raises(RuntimeError, match='identity changed'):
        guest.finish_restart('parent')
    guest.finish_restart('child')
    assert not (root / 'frozen.json').exists()


def test_original_activation_resumes_processes_and_ignores_reused_pids(tmp_path, monkeypatch):
    identity = tmp_path / 'identity'
    identity.write_text('parent')
    guest.atomic(tmp_path / 'frozen.json', {'uid': 'parent', 'processes': {'42': 'start', '43': 'old'}})
    monkeypatch.setattr(guest, 'ROOT', tmp_path)
    monkeypatch.setattr(guest, 'IDENTITY', identity)
    monkeypatch.setattr(guest, 'process_identity', lambda pid: ('start', 'T'))
    calls = []
    monkeypatch.setattr(guest.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    assert guest.activate() is False
    assert calls == [(42, guest.signal.SIGCONT)]


async def test_activation_retries_guest_restart_but_not_authentication_failures(transport, monkeypatch):
    sandbox, _ = transport
    original = guest.Handler.do_POST
    nonces = []
    def post(handler):
        nonces.append(handler.headers['X-Moyai-Nonce'])
        if len(nonces) == 1:
            handler.close_connection = True
            return
        if len(nonces) == 2:
            handler.reply(503, {'error': 'restarting'})
            return
        original(handler)
    monkeypatch.setattr(guest.Handler, 'do_POST', post)
    assert (await sandbox.request('/activate', {}))['boot_id'] == guest.BOOT_ID
    assert len(set(nonces)) == 3
    sandbox.actor.metadata.uid = 'wrong-actor'
    with pytest.raises(RuntimeError, match='401'):
        await sandbox.request('/activate', {})
    assert len(nonces) == 4


def test_clone_dispatch_restarts_before_accepting_work(tmp_path, monkeypatch):
    identity = tmp_path / 'identity'
    identity.write_text('child')
    guest.atomic(tmp_path / 'frozen.json', {'uid': 'parent', 'processes': {}})
    monkeypatch.setattr(guest, 'ROOT', tmp_path)
    monkeypatch.setattr(guest, 'IDENTITY', identity)
    calls = []
    monkeypatch.setattr(guest.os, 'execv', lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match='did not replace'):
        guest.dispatch('/activate', {})
    assert calls == [(sys.executable, [sys.executable, guest.__file__, 'restarted', 'child'])]
    assert (tmp_path / 'frozen.json').exists()


async def test_checkpoint_always_thaws_original_after_failed_tag():
    actor = pb.Actor(metadata={'atespace': 'tests', 'name': 'one', 'uid': 'actor-one'})
    backend = SubstrateProvider(Settings(_env_file=None))
    async def rpc(method, data, **kwargs):
        if method == 'CreateTag':
            raise RuntimeError('tag failure')
    backend.rpc = AsyncMock(side_effect=rpc)
    sandbox = Sandbox(backend, actor)
    sandbox._request = AsyncMock(return_value={})
    with pytest.raises(RuntimeError, match='tag failure'):
        await sandbox.snapshot_filesystem.aio()
    assert [call.args[0] for call in backend.rpc.call_args_list] == ['SuspendActor', 'CreateTag', 'ResumeActor']
    assert [call.args[0] for call in sandbox._request.call_args_list] == ['/freeze', '/activate']


async def test_retry_initializes_partial_actor_without_deleting_it_on_failure():
    from modal.exception import AlreadyExistsError
    actor = pb.Actor(metadata={'atespace': 'tests', 'name': 'one', 'uid': 'actor-one'})
    backend = SubstrateProvider(Settings(_env_file=None))
    async def rpc(method, data, **kwargs):
        if method == 'CreateActor':
            raise AlreadyExistsError('exists')
        return actor
    backend.rpc = AsyncMock(side_effect=rpc)
    backend.initialize = AsyncMock(side_effect=RuntimeError('temporary router outage'))
    with pytest.raises(RuntimeError):
        await backend.create(name='one')
    assert [call.args[0] for call in backend.rpc.call_args_list] == ['CreateActor', 'GetActor']
    backend.initialize.reset_mock(side_effect=True)
    sandbox = await backend.find('one', initialize=True, token='retry-token')
    assert sandbox.env['WORKSPACE_RUN_TOKEN'] == 'retry-token'
    backend.initialize.assert_awaited_once()
    backend.initialize.reset_mock()
    await backend.find('one')
    backend.initialize.assert_not_awaited()


async def test_initialize_sends_required_egress_ports():
    backend = SubstrateProvider(Settings(_env_file=None, substrate_egress_hosts='github.com,*.example.com'))
    backend.rpc = AsyncMock()
    sandbox = Sandbox(backend, pb.Actor(metadata={'atespace': 'tests', 'name': 'one', 'uid': 'actor-one'}))
    sandbox.request = AsyncMock()
    await backend.initialize(sandbox, timeout=120, apt_packages=())
    method, data = backend.rpc.call_args_list[0].args
    assert method == 'CreateActorEgressPolicy'
    policy = pb.CreateActorEgressPolicyRequest(**data).egress_policy
    for rule in (policy.rules[0].http, policy.rules[1].tls_passthrough):
        assert list(rule.hostnames) == ['github.com', '*.example.com']
    assert list(policy.rules[0].http.ports.numbers) == [80]
    assert policy.rules[1].tls_passthrough.ports.HasField('all')


def test_repeated_activation_does_not_extend_original_lease(transport):
    sandbox, _ = transport
    guest.dispatch('/activate', {'expires_at': 1234})
    guest.dispatch('/activate', {'expires_at': 5678})
    assert json.loads((guest.ROOT / 'lease.json').read_text())['expires_at'] == 1234


def test_initial_signing_key_does_not_pin_environment_defaults(workspace):
    app, _ = workspace
    from app.sandbox_settings import SandboxSettings
    settings = Settings(_env_file=None, data_dir=app.state.settings.data_dir, modal_token_id='new-env-token')
    SandboxSettings(app.state.store, settings, app.state.security)
    assert settings.modal_token_id == 'new-env-token'


async def test_control_tls_negotiates_http2_and_verifies_private_ca(tmp_path):
    import datetime
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from grpclib.exceptions import StreamTerminatedError
    private = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(minutes=10))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .sign(private, hashes.SHA256()))
    certificate = cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / 'cert.pem').write_text(certificate)
    (tmp_path / 'key.pem').write_bytes(private.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / 'cert.pem', tmp_path / 'key.pem')
    context.set_alpn_protocols(['h2'])
    negotiated = asyncio.get_running_loop().create_future()
    async def connected(reader, writer):
        negotiated.set_result(writer.get_extra_info('ssl_object').selected_alpn_protocol())
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(connected, '127.0.0.1', 0, ssl=context)
    async with server:
        settings = Settings(_env_file=None, substrate_api_url=f'https://localhost:{server.sockets[0].getsockname()[1]}',
                            substrate_ca_cert=certificate)
        with pytest.raises(StreamTerminatedError):
            await SubstrateProvider(settings).rpc('GetActorTemplate', {'actor_template': {'atespace':'tests','name':'one'}}, timeout=5)
        assert await negotiated == 'h2'


@pytest.mark.parametrize('tab', ['', 'https://github.com/BerriAI/moyai/pull/42'])
async def test_computer_rpc_uses_signed_transport_without_execution_journals(transport, monkeypatch, tab):
    sandbox, tmp = transport
    calls = []
    def request(body, *, start):
        calls.append((body, start))
        return {'surface': 'desktop', 'controller': body.get('actor', '')}
    monkeypatch.setattr(guest.computer, 'request', request)
    state = {'action': 'state', 'tab': tab}
    assert (await sandbox.computer_request(state))['surface'] == 'desktop'
    body = {**Command(action='input', tab=tab, args={'events': [{'type': 'text', 'text': 'test input'}]}).model_dump(),
            'actor': 'test-person'}
    assert (await sandbox.computer_request(body))['controller'] == 'test-person'
    assert calls == [(state, False), (body, True)]
    assert not (tmp / 'runtime' / 'jobs').exists()


async def test_computer_rpc_does_not_replay_an_unconfirmed_input(transport, monkeypatch):
    import httpx
    sandbox, _ = transport
    calls = []
    monkeypatch.setattr(guest.computer, 'request', lambda body, **kwargs: calls.append(body) or {})
    def drop_reply(handler, code, value):
        handler.close_connection = True
    monkeypatch.setattr(guest.Handler, 'reply', drop_reply)
    with pytest.raises(httpx.TransportError):
        await sandbox.computer_request({'action': 'input', 'actor': 'person', 'args': {'events': []}})
    assert len(calls) == 1


@pytest.mark.parametrize('status', [404, 409])
async def test_legacy_computer_route_requires_restart_without_fallback(transport, monkeypatch, status):
    sandbox, _ = transport
    calls = []
    def legacy(handler):
        calls.append(handler.path)
        handler.reply(status, {'error': 'Unknown operation'})
    monkeypatch.setattr(guest.Handler, 'do_POST', legacy)
    with pytest.raises(RuntimeError, match='Restart this workspace'):
        await sandbox.computer_request({'action': 'input', 'args': {'events': []}})
    assert calls == ['/computer']


async def test_computer_rpc_bounds_and_uid_recheck_prevent_forwarding(transport, monkeypatch):
    sandbox, _ = transport
    calls = []
    monkeypatch.setattr(guest.computer, 'request', lambda body, **kwargs: calls.append(body) or {})
    for body, status in [({'action': 'input', 'args': {'text': 'x' * 65536}}, '413'),
                         ({'action': 'state', 'unexpected': True}, 'Restart this workspace'),
                         *[({'action': 'state', 'tab': tab}, 'Restart this workspace') for tab in
                           (None, 42, 'https://github.com/BerriAI/moyai/pull/01', 'https://example.com/')]]:
        with pytest.raises(RuntimeError, match=status):
            await sandbox.computer_request(body)
    assert calls == []
    restore = {'action': 'state', 'args': {'browser': 'restore', 'scope': 'a' * 32,
        'state': {'storage': {'cookies': [], 'origins': [{'origin': 'https://example.test',
            'localStorage': [{'name': 'saved-login', 'value': 'x' * 90000}]}]}, 'pages': [], 'active': 0}}}
    assert await sandbox.computer_request(restore) == {}
    assert calls == [restore]  # The signed private channel admits a realistic saved browser state.
    def replaced(body, **kwargs):
        guest.IDENTITY.write_text('replacement-actor')
        calls.append(body)
        return {}
    monkeypatch.setattr(guest.computer, 'request', replaced)
    with pytest.raises(RuntimeError, match='401'):
        await sandbox.computer_request({'action': 'state'})
    assert len(calls) == 2
    # The response reaches the client before the handler's context exits.
    with guest.COMPUTER_IDLE:
        assert guest.COMPUTER_IDLE.wait_for(lambda: guest.COMPUTER_PENDING == 0, timeout=5)


async def test_computer_rpc_allows_guest_reads_but_freeze_waits_for_completion(transport, monkeypatch):
    sandbox, tmp = transport
    entered, release, waiting, frozen = (threading.Event() for _ in range(4))
    def request(body, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'surface': 'desktop'}
    original_wait = guest.COMPUTER_IDLE.wait_for
    def wait(predicate, timeout):
        waiting.set()
        return original_wait(predicate, timeout)
    def freeze_processes():
        frozen.set()
        guest.atomic(guest.ROOT / 'frozen.json', {'uid': 'actor-one', 'processes': {}})
    monkeypatch.setattr(guest.computer, 'request', request)
    monkeypatch.setattr(guest.COMPUTER_IDLE, 'wait_for', wait)
    monkeypatch.setattr(guest, 'freeze_processes', freeze_processes)
    action = asyncio.create_task(sandbox.computer_request({'action': 'input', 'args': {'events': []}}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # Another server worker has its own provider lock but shares this guest.
        result = await asyncio.wait_for(sandbox._request_once('/file/stat', {'path': str(tmp / 'identity')}), 2)
        assert result['size'] > 0
        checkpoint = asyncio.create_task(sandbox._request_once('/freeze', {}))
        assert await asyncio.to_thread(waiting.wait, 3)
        assert not frozen.is_set()
        with pytest.raises(RuntimeError, match='Restart this workspace'):
            await sandbox._request_once('/computer', {'action': 'state'})
    finally:
        release.set()
    await action
    await checkpoint
    assert frozen.is_set() and guest.COMPUTER_PENDING == 0 and not guest.COMPUTER_FREEZING


def test_busy_computer_aborts_freeze_before_stopping_processes(monkeypatch):
    calls = []
    monkeypatch.setattr(guest, 'COMPUTER_PENDING', 1)
    monkeypatch.setattr(guest.COMPUTER_IDLE, 'wait_for', lambda predicate, timeout: False)
    monkeypatch.setattr(guest, 'freeze_processes', lambda: calls.append('stopped'))
    with pytest.raises(RuntimeError, match='Computer action is still running'):
        guest.freeze()
    assert not calls and not guest.COMPUTER_FREEZING
