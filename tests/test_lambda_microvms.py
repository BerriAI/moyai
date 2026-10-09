"""AWS SDK contracts plus adversarial provider/checkpoint/lifecycle cases."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
from botocore.stub import Stubber
import httpx
from modal.exception import NotFoundError
import pytest

from app.config import Settings
from app.sandboxes import provider, provider_for_id
from app.sandboxes.lambda_microvm import LambdaProvider, Sandbox, MAX_ARCHIVE
from sandbox import lambda_checkpoint as checkpoint
from sandbox.continuation import RotationDeadline


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path, sandbox_provider='lambda',
        lambda_region='us-east-1', lambda_image='arn:aws:lambda:us-east-1:123456789012:microvm-image:moyai',
        lambda_image_version='1.0', lambda_checkpoint_bucket='moyai-test', lambda_checkpoint_prefix='test')


def vm(state='RUNNING'):
    return {'microvmId': 'mvm-test', 'state': state, 'startedAt': 1000,
        'maximumDurationInSeconds': 28800, 'endpoint': 'mvm-test.lambda-microvm.us-east-1.on.aws',
        'imageArn': 'arn:aws:lambda:us-east-1:123456789012:microvm-image:moyai', 'imageVersion': '1.0'}


def test_configuration_is_provider_specific(settings):
    assert settings.missing_sandbox() == []
    assert settings.missing_sandbox('modal')
    assert provider(settings).name == 'lambda'
    assert provider_for_id('lambda:us-east-1:mvm-test') == 'lambda'
    assert provider_for_id('im-legacy') == 'modal'
    assert settings.sandbox_lifetime_seconds() == 28800
    assert settings.sandbox_rotation_for() == 25200
    assert settings.sandbox_rotation_for('modal') == 82800
    settings.run_timeout_seconds = 82800
    assert settings.sandbox_lifetime_seconds() == 28800
    settings.sandbox_rotation_seconds = 300
    assert settings.sandbox_rotation_for() == 300


def test_large_checkpoint_hash_preserves_byte_order_and_handles_short_reads(tmp_path, monkeypatch):
    import hashlib
    data = b''.join(bytes([part]) * (3 * 1024 * 1024) for part in range(7))
    path = tmp_path / 'large-executable'
    path.write_bytes(data)
    pread = os.pread
    monkeypatch.setattr(checkpoint.os, 'pread', lambda fd, size, offset: pread(fd, min(size, 1024 * 1024), offset))
    assert checkpoint.digest(path).digest() == hashlib.sha256(data).digest()


def test_cold_checkpoint_budget_reaches_the_guest_watchdog(tmp_path, monkeypatch):
    from sandbox import lambda_guest as guest
    settings = Settings(_env_file=None, snapshot_timeout_seconds=900)
    monkeypatch.setattr(guest, 'ROOT', tmp_path)
    monkeypatch.setattr(guest, 'JOBS', {})
    monkeypatch.setattr(guest, 'ACTIVE', set())
    monkeypatch.setattr(guest, 'BUSY', None)
    threads = []
    def thread(**kwargs):
        threads.append(kwargs)
        return SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(guest.threading, 'Thread', thread)
    identity = 'a' * 32
    guest.start_job('/checkpoint', {'id': identity, 'timeout': settings.snapshot_timeout_seconds})
    watchdog = next(t for t in threads if t['target'] is guest.expire_checkpoint)
    assert watchdog['args'] == (identity, 900)


def test_absolute_rotation_includes_boot_and_previous_turns_and_waits_for_boundary():
    now, stops = [0], []
    deadline = RotationDeadline(600, rotation_at=20, wall_clock=lambda: now[0])
    now[0] = 21
    assert not stops  # No timer can interrupt an external action.
    deadline.step(SimpleNamespace(interrupt=lambda: stops.append(True)))
    assert stops == [True]  # No first-round exemption on an old VM.
    pending = {'interrupted': True, 'messages': [{'role': 'assistant', 'tool_calls': [{'id': 'write'}]}]}
    assert not deadline.can_continue(pending)
    pending['messages'].append({'role': 'tool', 'tool_call_id': 'write', 'content': 'sent'})
    assert deadline.can_continue(pending)


async def test_sdk_contract_and_proxy_auth(settings, monkeypatch):
    backend = LambdaProvider(settings)
    sdk = boto3.client('lambda-microvms', region_name='us-east-1',
                       aws_access_key_id='test', aws_secret_access_key='test')
    monkeypatch.setattr(backend.session, 'client', lambda *args, **kwargs: sdk)
    with Stubber(sdk) as stub:
        stub.add_response('get_microvm', vm(), {'microvmIdentifier': 'mvm-test'})
        stub.add_response('create_microvm_auth_token', {'authToken': {'X-aws-proxy-auth': 'jwe-secret'}},
            {'microvmIdentifier': 'mvm-test', 'expirationInMinutes': 15, 'allowedPorts': [{'port': 80}]})
        sandbox = await backend.get('lambda:us-east-1:mvm-test')
        calls = []
        def response(request):
            calls.append(request)
            assert request.headers['X-aws-proxy-auth'] == 'jwe-secret'
            assert request.headers['X-aws-proxy-port'] == '80'
            assert request.url == 'https://mvm-test.lambda-microvm.us-east-1.on.aws/health'
            return httpx.Response(200, json={'version': 1})
        client = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
        assert await sandbox.request('/health', {}) == {'version': 1}
        assert await sandbox.request('/health', {}) == {'version': 1}
        assert len(calls) == 2
        stub.assert_no_pending_responses()


@pytest.mark.parametrize('path', ['/start', '/computer', '/checkpoint', '/initialize'])
@pytest.mark.parametrize('failure', ['connection', 'gateway'])
async def test_uncertain_mutation_is_not_retried(settings, monkeypatch, path, failure):
    backend = LambdaProvider(settings)
    sandbox = Sandbox(backend, vm())
    backend.aws = AsyncMock(return_value={'authToken': {'X-aws-proxy-auth': 'jwe'}})
    calls = []
    def response(request):
        calls.append(request)
        if failure == 'gateway':
            return httpx.Response(502)
        raise httpx.ReadError('lost acknowledgement')
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    with pytest.raises(httpx.ReadError if failure == 'connection' else RuntimeError):
        await sandbox.request(path, {'id': 'uncertain-action'})
    assert len(calls) == 1


@pytest.mark.parametrize('path', ['/health', '/job', '/read', '/file/read', '/file/stat'])
async def test_transient_gateway_failure_retries_only_the_same_observation(settings, monkeypatch, path):
    backend = LambdaProvider(settings)
    sandbox = Sandbox(backend, vm())
    backend.aws = AsyncMock(return_value={'authToken': {'X-aws-proxy-auth': 'jwe'}})
    calls = []
    def response(request):
        calls.append(request.content)
        return httpx.Response(502) if len(calls) == 1 else httpx.Response(200, json={'state': 'done'})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(response), **kw))
    assert await sandbox.request(path, {'id': 'existing-job', 'stdout': 128}) == {'state': 'done'}
    assert calls == [calls[0], calls[0]]


@pytest.mark.parametrize('endpoint', ['https://evil.test', 'http://mvm-test.lambda-microvm.us-east-1.on.aws',
    'https://user:password@mvm-test.lambda-microvm.us-east-1.on.aws',
    'https://mvm-test.lambda-microvm.us-east-1.on.aws/other'])
async def test_never_sends_endpoint_tokens_to_other_hosts(settings, endpoint):
    backend = LambdaProvider(settings)
    backend.aws = AsyncMock(return_value={'authToken': {'X-aws-proxy-auth': 'secret'}})
    sandbox = Sandbox(backend, {**vm(), 'endpoint': endpoint})
    with pytest.raises(ValueError, match='endpoint'):
        await sandbox.request('/health', {})


class MemoryAWS:
    def __init__(self, backend):
        self.objects, self.calls, self.lost = {}, [], True
        self.vm = vm()
        backend.aws = self.call
    async def call(self, service, method, **kw):
        self.calls.append((method, kw))
        if method == 'put_object':
            if kw.get('IfNoneMatch') == '*' and kw['Key'] in self.objects:
                raise RuntimeError('AWS put_object failed: PreconditionFailed')
            self.objects[kw['Key']] = kw['Body']
            return {}
        if method == 'get_object':
            if kw['Key'] not in self.objects:
                raise NotFoundError('missing')
            return {'Body': io.BytesIO(self.objects[kw['Key']])}
        if method == 'run_microvm':
            if self.lost:
                self.lost = False
                raise ConnectionError('AWS accepted RunMicrovm but its response was lost')
            return self.vm
        if method == 'get_microvm':
            return self.vm
        raise AssertionError(method)


async def test_lost_create_ack_reuses_exact_request_and_disables_endpoint_idle(settings, monkeypatch):
    backend = LambdaProvider(settings)
    aws = MemoryAWS(backend)
    monkeypatch.setattr(backend, 'initialize', AsyncMock())
    with pytest.raises(ConnectionError):
        await backend.create(name='durable-session')
    restored = await backend.find('durable-session', initialize=True)
    requests = [kw for method, kw in aws.calls if method == 'run_microvm']
    assert requests[0] == requests[1]
    assert requests[0]['maximumDurationInSeconds'] == 28800
    assert requests[0]['idlePolicy'] == {'autoResumeEnabled': False,
        'maxIdleDurationSeconds': 28800, 'suspendedDurationSeconds': 28800}
    assert restored.object_id == 'lambda:us-east-1:mvm-test'
    assert restored.started_at == 1000
    await backend.create(name='durable-session')
    assert len([m for m, _ in aws.calls if m == 'run_microvm']) == 2


async def test_restore_is_from_pinned_image_and_never_from_suspended_vm(settings, monkeypatch):
    backend = LambdaProvider(settings)
    aws = MemoryAWS(backend)
    aws.lost = False
    checkpoint_id = 'lambda:us-east-1:moyai-test:test/checkpoints/abc.json'
    manifest = {'version': 1, 'image': 'old-image', 'image_version': '0.5',
        'key': 'test/checkpoints/abc.tar.gz', 'size': 100, 'sha256': 'a' * 64}
    aws.objects['test/checkpoints/abc.json'] = json.dumps(manifest).encode()
    monkeypatch.setattr(backend, 'initialize', AsyncMock())
    await backend.create(name='child', snapshot_id=checkpoint_id)
    request = next(kw for method, kw in aws.calls if method == 'run_microvm')
    assert request['imageIdentifier'] == 'old-image' and request['imageVersion'] == '0.5'
    assert not any('resume' in method or 'suspend' in method for method, _ in aws.calls)
    with pytest.raises(ValueError, match='installation'):
        await backend.checkpoint(checkpoint_id.replace('moyai-test', 'other-bucket'))
    with pytest.raises(ValueError):
        await backend.create(snapshot_id='im-modal')


async def test_failed_initialization_confirms_termination(settings, monkeypatch):
    backend = LambdaProvider(settings)
    aws = MemoryAWS(backend)
    aws.lost = False
    monkeypatch.setattr(backend, 'initialize', AsyncMock(side_effect=ValueError('bad archive')))
    terminated = AsyncMock()
    monkeypatch.setattr(Sandbox, 'terminate', SimpleNamespace(aio=terminated))
    with pytest.raises(ValueError, match='bad archive'):
        await backend.create(name='failed')
    terminated.assert_awaited_once()


async def test_termination_waits_for_terminal_state(settings, monkeypatch):
    backend = LambdaProvider(settings)
    backend.aws = AsyncMock(side_effect=[{}, vm('TERMINATING'), vm('TERMINATING'), vm('TERMINATED')])
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    await Sandbox(backend, vm()).terminate.aio()
    assert [c.args[1] for c in backend.aws.await_args_list] == ['terminate_microvm', 'get_microvm', 'get_microvm', 'get_microvm']


@pytest.mark.parametrize('failure', ['upload', 'checksum', 'manifest'])
async def test_snapshot_not_published_until_s3_confirms(settings, failure):
    backend = LambdaProvider(settings)
    sandbox = Sandbox(backend, vm())
    meta = {'size': 120, 'sha256': 'a' * 64, 'checksum': 'check'}
    sandbox.job = AsyncMock(side_effect=[meta, RuntimeError('upload failed')] if failure == 'upload' else [meta, {}])
    sandbox._request = AsyncMock(return_value={})
    backend.url = AsyncMock(return_value='https://s3.example/signed')
    backend.aws = AsyncMock(return_value={'ContentLength': 120, 'ChecksumSHA256': 'wrong' if failure == 'checksum' else 'check'})
    backend.write = AsyncMock(side_effect=RuntimeError('manifest failed') if failure == 'manifest' else None)
    with pytest.raises(RuntimeError):
        await sandbox.snapshot_filesystem.aio()
    assert backend.write.await_count == int(failure == 'manifest')
    assert sandbox._request.call_args.args[0] == '/checkpoint/finish'


def populate(root):
    for name, text in {'workspace/.gitignore': 'node_modules\n', 'workspace/node_modules/lib/index.js': 'installed',
        'workspace/untracked': 'untracked', 'session/conversation.json': '{"answer":"saved"}',
        'root/.claude/projects/native.jsonl': 'native conversation', 'usr/local/bin/custom-tool': '#!/bin/sh\necho works',
        'usr/lib/installed-package': 'system dependency', 'etc/project.conf': 'configuration',
        'opt/env/lib/dependency.py': 'dependency', 'tmp/hermes-home/state': 'hermes state'}.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / 'usr/local/bin/custom-tool').chmod(0o755)
    (root / 'workspace/link').symlink_to('untracked')
    (root / 'workspace/hardlink').hardlink_to(root / 'workspace/untracked')


def test_checkpoint_preserves_full_workspace_dependencies_history_deletions_and_modes(tmp_path):
    source, target, work = (tmp_path / name for name in ('source', 'target', 'work'))
    for p in (source, target, work):
        p.mkdir()
    populate(source)
    populate(target)
    old = source / 'usr/lib/deleted-package'
    old.write_text('deleted')
    (target / 'usr/lib/deleted-package').write_text('deleted')
    baseline = work / 'base.json'
    checkpoint.baseline(source, baseline)
    old.unlink()
    (source / 'workspace/untracked').write_text('changed after build')
    private = source / 'var/lib/moyai-runtime/jobs/secret'
    private.parent.mkdir(parents=True)
    private.write_text('parent capability')
    archive = work / 'archive.tar.gz'
    result = checkpoint.pack(archive, root=source, baseline_path=baseline, max_bytes=MAX_ARCHIVE)
    checkpoint.restore(archive, root=target, expected_sha256=result['sha256'])
    for name in checkpoint.inventory(source):
        a, b = source / name, target / name
        if a.is_file():
            assert a.read_bytes() == b.read_bytes(), name
            assert a.stat().st_mode == b.stat().st_mode, name
    assert not (target / 'usr/lib/deleted-package').exists()
    assert not (target / 'var/lib/moyai-runtime').exists()
    assert (target / 'workspace/link').is_symlink()
    assert (target / 'workspace/hardlink').stat().st_ino == (target / 'workspace/untracked').stat().st_ino
    with pytest.raises(ValueError, match='checksum'):
        checkpoint.restore(archive, root=target, expected_sha256='0' * 64)
    with pytest.raises(RuntimeError, match='limit'):
        checkpoint.pack(archive, root=source, baseline_path=baseline, max_bytes=10)


from test_durable import durable, drive


def lambda_lifecycle(durable, monkeypatch):
    manager, cloud, run_id = durable
    manager.store.execute("UPDATE runs SET sandbox_provider='lambda' WHERE id=?", (run_id,))
    # Exercise the real durable state machine with AWS-prefixed handles. The
    # provider contract/HTTP service is covered separately by SDK and Docker.
    old_create = cloud.create
    async def create(**kwargs):
        machine = await old_create(**kwargs)
        machine.object_id = f'lambda:us-east-1:mvm-{len(cloud.machines)}'
        machine.started_at = 1000
        return machine
    async def find(name, **kwargs):
        return await cloud.from_name('unused', name)
    cloud.create = create
    backend = SimpleNamespace(name='lambda', create=create, find=find, get=cloud.from_id)
    monkeypatch.setattr(manager, 'provider', lambda *a, **kw: backend)
    return manager, cloud, run_id


async def test_aws_runner_uses_actual_start_and_waits_for_confirmed_termination(durable, monkeypatch):
    manager, cloud, run_id = lambda_lifecycle(durable, monkeypatch)
    cloud.continue_once, cloud.saving_before_answer = True, False
    await drive(manager, run_id, phase='checkpointed')
    assert manager.state(run_id)['machine_started'] == 1000
    assert cloud.machines[0].spec['rotation_at'] == 26200
    wait = cloud.machines[0].wait
    cloud.machines[0].wait = SimpleNamespace(aio=AsyncMock(side_effect=TimeoutError('not terminated')))
    with pytest.raises(TimeoutError):
        await manager.advance(run_id)
    assert len(cloud.machines) == 1
    assert manager.state(run_id)['phase'] == 'checkpointed'
    # A failed wait does not license creation. Reconfirm the original machine.
    cloud.machines[0].alive = True
    cloud.machines[0].wait = wait
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'provision'
    await drive(manager, run_id)
    assert len(cloud.machines) == 2 and len(cloud.launches) == 2


async def test_aws_save_failure_preserves_answer_and_previous_checkpoint(durable, monkeypatch):
    manager, cloud, run_id = lambda_lifecycle(durable, monkeypatch)
    manager.store.update_run(run_id, snapshot_id='previous-checkpoint')
    await drive(manager, run_id, phase='save')
    manager.store.enqueue_message(run_id, 'Must not run', 'queued-next')
    cloud.save_failures = 3
    for _ in range(2):
        with pytest.raises(TimeoutError):
            await manager.advance(run_id)
        assert manager.store.run(run_id)['summary'] == 'Saved answer'
    await drive(manager, run_id)
    assert manager.store.run(run_id)['snapshot_id'] == 'previous-checkpoint'
    assert 'Saved answer' in manager.store.messages(run_id)[1]['content']
    assert manager.store.messages(run_id)[-1]['status'] == 'cancelled'
    assert len(cloud.launches) == 1


async def test_aws_dead_machine_does_not_replay_unfinished_actions(durable, monkeypatch):
    manager, cloud, run_id = lambda_lifecycle(durable, monkeypatch)
    await drive(manager, run_id, phase='monitor')
    cloud.machines[0].alive = False
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'interrupted'
    assert len(cloud.launches) == len(cloud.machines) == 1


def test_registry_cannot_be_repointed_while_sessions_exist(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(LambdaProvider, 'check', AsyncMock(return_value='AWS sandbox verified'))
    values = {'lambda_region': 'us-east-1', 'lambda_image': 'test-image', 'lambda_image_version': '1.0',
              'lambda_checkpoint_bucket': 'test-bucket', 'lambda_checkpoint_prefix': 'test'}
    response = client.put('/api/settings/sandboxes', json={'provider': 'lambda', 'revision': 0, 'values': values})
    assert response.status_code == 200, response.text
    run = app.state.store.create_run('AWS task', '', 'modal', [], chat_enabled=True)
    app.state.store.update_run(run['id'], sandbox_id='lambda:us-east-1:mvm-test')
    response = client.put('/api/settings/sandboxes', json={'provider': 'lambda', 'revision': 1,
        'values': {**values, 'lambda_checkpoint_bucket': 'different'}})
    assert response.status_code == 409
    assert app.state.settings.lambda_checkpoint_bucket == 'test-bucket'


from test_workspace import workspace


async def test_checkpoint_serializes_other_handles_for_same_vm(settings):
    backend = LambdaProvider(settings)
    saving, reader = Sandbox(backend, vm()), Sandbox(backend, vm())
    started, finish = asyncio.Event(), asyncio.Event()
    async def checkpoint_call(timeout):
        started.set()
        await finish.wait()
        return SimpleNamespace(object_id='saved')
    saving.checkpoint = checkpoint_call
    reader._request = AsyncMock(return_value={'data': ''})
    task = asyncio.create_task(saving.snapshot_filesystem.aio())
    await started.wait()
    pending = asyncio.create_task(reader.request('/read', {}))
    await asyncio.sleep(.01)
    reader._request.assert_not_awaited()
    finish.set()
    await task
    await pending
    reader._request.assert_awaited_once()


def test_checkpoint_hashes_shared_data_once_and_detects_same_metadata_edits(tmp_path, monkeypatch):
    source, target, work = (tmp_path / name for name in ('source', 'target', 'work'))
    for root in (source, target, work):
        root.mkdir()
    for root in (source, target):
        (root / 'file').write_text('old-data')
        (root / 'linked').hardlink_to(root / 'file')
    base = work / 'base.json'
    checkpoint.baseline(source, base)
    before = (source / 'file').stat()
    (source / 'file').write_text('new-data')
    os.utime(source / 'file', ns=(before.st_atime_ns, before.st_mtime_ns))
    original_digest, reads = checkpoint.digest, []
    def digest(path):
        if path.parent == source:
            reads.append(path)
        return original_digest(path)
    monkeypatch.setattr(checkpoint, 'digest', digest)
    archive = work / 'checkpoint.tar.gz'
    result = checkpoint.pack(archive, root=source, baseline_path=base, max_bytes=MAX_ARCHIVE)
    assert len(reads) == 1
    checkpoint.restore(archive, root=target, expected_sha256=result['sha256'])
    assert (target / 'file').read_text() == (target / 'linked').read_text() == 'new-data'
    assert (target / 'file').stat().st_ino == (target / 'linked').stat().st_ino
    # A later checkpoint must hash again even with unchanged size and mtime.
    (source / 'file').write_text('next-one')
    os.utime(source / 'file', ns=(before.st_atime_ns, before.st_mtime_ns))
    result = checkpoint.pack(archive, root=source, baseline_path=base, max_bytes=MAX_ARCHIVE)
    assert len(reads) == 2
    checkpoint.restore(archive, root=target, expected_sha256=result['sha256'])
    assert (target / 'linked').read_text() == 'next-one'


def test_delta_restores_changed_file_types_and_new_hardlinks(tmp_path):
    source, target, work = (tmp_path / name for name in ('source', 'target', 'work'))
    for p in (source, target, work):
        p.mkdir()
    for root in (source, target):
        (root / 'directory').mkdir()
        (root / 'directory/child').write_text('old')
        (root / 'file').write_text('old')
        (root / 'unchanged').write_text('original')
    base = work / 'base.json'
    checkpoint.baseline(source, base)
    (source / 'directory/child').unlink()
    (source / 'directory').rmdir()
    (source / 'directory').write_text('now a file')
    (source / 'file').unlink()
    (source / 'file').mkdir()
    (source / 'file/child').write_text('now a directory')
    (source / 'new-hardlink').hardlink_to(source / 'unchanged')
    archive = work / 'checkpoint.tar.gz'
    meta = checkpoint.pack(archive, root=source, baseline_path=base, max_bytes=MAX_ARCHIVE)
    checkpoint.restore(archive, root=target, expected_sha256=meta['sha256'])
    assert (target / 'directory').read_text() == 'now a file'
    assert (target / 'file/child').read_text() == 'now a directory'
    assert (target / 'new-hardlink').stat().st_ino == (target / 'unchanged').stat().st_ino


def test_image_package_contains_no_server_secrets_and_uses_real_sdk_shape(tmp_path):
    import zipfile
    from botocore.validate import validate_parameters
    from scripts.lambda_image import image_request, package
    target = tmp_path / 'image.zip'
    package(target)
    with zipfile.ZipFile(target) as archive:
        assert archive.read('Dockerfile').decode().endswith('FROM lambda-workspace AS aws-image\n')
        assert 'sandbox/lambda_guest.py' in archive.namelist()
        assert all(name == 'Dockerfile' or name.startswith('sandbox/') for name in archive.namelist())
        assert not any(name.endswith('.pyc') or '.env' in name for name in archive.namelist())
    client = boto3.client('lambda-microvms', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test')
    request = image_request(region='us-east-1', name='moyai', bucket='artifacts', key='image.zip',
                            role='arn:aws:iam::123456789012:role/build')
    validate_parameters(request, client.meta.service_model.operation_model('CreateMicrovmImage').input_shape)


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_aws_image_ready_hook_works_before_bootstrap_without_a_body(method):
    from http.server import ThreadingHTTPServer
    import threading
    from sandbox.lambda_guest import Handler
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        response = httpx.request(method, url + '/aws/lambda-microvms/runtime/v1/ready')
        assert response.status_code == 200
        assert response.json() == {'version': 1, 'bootstrapped': False}
        assert httpx.get(url + '/unknown').status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
