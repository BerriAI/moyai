"""AWS Lambda MicroVMs with independent S3 filesystem checkpoints.

Suspend/resume is deliberately not a checkpoint. Every restoration starts a new
VM from the checkpoint's pinned image and restores files, never processes.
"""
import asyncio
from contextlib import closing
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx
from modal.exception import NotFoundError

from .substrate import Filesystem, Process, operation

_LOCKS = WeakValueDictionary()

MAX_LIFETIME = 28800
MAX_ARCHIVE = 4 * 1024 ** 3  # A bounded single S3 PUT; oversized saves fail closed.


class TransientEndpointError(RuntimeError):
    """AWS could not deliver an endpoint response; execution is ambiguous."""


class LambdaProvider:
    name = 'lambda'

    def __init__(self, settings):
        self.settings = settings
        self.session = boto3.Session(profile_name=settings.lambda_profile or None,
                                     region_name=settings.lambda_region)

    async def aws(self, service, method, **kwargs):
        def call():
            # Handles are short-lived (including in the durable runner). Close
            # HTTP pools instead of accumulating one per poll/stream read.
            with closing(self.session.client(service, config=Config(connect_timeout=10, read_timeout=30,
                    retries={'mode': 'standard', 'total_max_attempts': 3}))) as client:
                return getattr(client, method)(**kwargs)
        try:
            return await asyncio.to_thread(call)
        except ClientError as exc:
            code = exc.response['Error']['Code']
            if code in {'NoSuchKey', '404', 'ResourceNotFoundException'}:
                raise NotFoundError('AWS sandbox or checkpoint not found') from None
            # AWS response bodies can contain presigned URLs. Keep them out of
            # run events and Temporal failure history.
            raise RuntimeError('AWS ' + method + ' failed: ' + code) from None

    def key(self, suffix):
        return self.settings.lambda_checkpoint_prefix + '/' + suffix

    async def read(self, key):
        result = await self.aws('s3', 'get_object', Bucket=self.settings.lambda_checkpoint_bucket, Key=key)
        body = result['Body']
        try:
            data = await asyncio.to_thread(body.read, 1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError('AWS checkpoint manifest is too large')
            return json.loads(data)
        finally:
            body.close()

    async def write(self, key, value, **kwargs):
        return await self.aws('s3', 'put_object', Bucket=self.settings.lambda_checkpoint_bucket,
            Key=key, Body=json.dumps(value).encode(), ContentType='application/json',
            ServerSideEncryption='AES256', **kwargs)

    async def url(self, method, key, **kwargs):
        return await self.aws('s3', 'generate_presigned_url', ClientMethod=method,
            Params={'Bucket': self.settings.lambda_checkpoint_bucket, 'Key': key, **kwargs}, ExpiresIn=900)

    def registry_key(self, name):
        return self.key('machines/' + hashlib.sha256(name.encode()).hexdigest() + '.json')

    async def checkpoint(self, identity):
        kind, region, bucket, key = identity.split(':', 3)
        if (kind != 'lambda' or region != self.settings.lambda_region or
                bucket != self.settings.lambda_checkpoint_bucket or
                not key.startswith(self.key('checkpoints/')) or not key.endswith('.json') or '..' in key):
            raise ValueError('Checkpoint belongs to another AWS installation')
        value = await self.read(key)
        if (value.get('version') != 1 or not value.get('image') or not value.get('image_version')
                or not 0 < value.get('size', 0) <= MAX_ARCHIVE or
                len(value.get('sha256', '')) != 64 or value.get('key') != key[:-5] + '.tar.gz'):
            raise ValueError('Invalid AWS checkpoint manifest')
        return value

    async def get(self, identity):
        kind, region, vm_id = identity.split(':')
        if kind != 'lambda' or region != self.settings.lambda_region:
            raise ValueError('Sandbox belongs to another AWS region')
        vm = await self.aws('lambda-microvms', 'get_microvm', microvmIdentifier=vm_id)
        return Sandbox(self, vm)

    async def find(self, name, *, initialize=False, token='', timeout=86400, apt_packages=()):
        record = await self.read(self.registry_key(name))
        if 'vm_id' not in record:
            if not initialize:
                raise RuntimeError('AWS creation outcome is unconfirmed; retry provisioning to resolve it')
            return await self.launch(name, record, token)
        sandbox = await self.get('lambda:' + self.settings.lambda_region + ':' + record['vm_id'])
        sandbox.env = {'WORKSPACE_RUN_TOKEN': token} if token else {}
        if initialize:
            await self.initialize(sandbox, record)
        return sandbox

    async def create(self, *, name=None, snapshot_id='', token='', timeout=86400, memory=4096, apt_packages=()):
        name = name or 'moyai-' + uuid4().hex
        manifest = await self.checkpoint(snapshot_id) if snapshot_id else None
        region = self.settings.lambda_region
        record = {'created_at': time.time(), 'snapshot_id': snapshot_id, 'apt_packages': list(apt_packages),
                  'request': {'clientToken': uuid4().hex,
                    'imageIdentifier': manifest['image'] if manifest else self.settings.lambda_image,
                    'imageVersion': manifest['image_version'] if manifest else self.settings.lambda_image_version,
                    'maximumDurationInSeconds': min(timeout, MAX_LIFETIME),
                    # Never let outbound model calls or background commands be
                    # mistaken for idleness. Moyai owns idle release after save.
                    'idlePolicy': {'autoResumeEnabled': False, 'maxIdleDurationSeconds': MAX_LIFETIME,
                                   'suspendedDurationSeconds': MAX_LIFETIME},
                    'ingressNetworkConnectors': [f'arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:ALL_INGRESS'],
                    'egressNetworkConnectors': [self.settings.lambda_egress_connector or
                        f'arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS']}}
        if self.settings.lambda_execution_role_arn:
            record['request']['executionRoleArn'] = self.settings.lambda_execution_role_arn
        try:
            await self.write(self.registry_key(name), record, IfNoneMatch='*')
        except RuntimeError as exc:
            if not str(exc).endswith(('PreconditionFailed', 'ConditionalRequestConflict')):
                raise
            return await self.find(name, initialize=True, token=token)
        return await self.launch(name, record, token)

    async def launch(self, name, record, token):
        # Do not reuse an idempotency key indefinitely after an ambiguous create.
        # The operator can inspect the orphan; an expired task is not replayed.
        if time.time() - record['created_at'] >= 3600:
            raise RuntimeError('AWS creation was not confirmed within one hour; inspect it before retrying')
        vm = await self.aws('lambda-microvms', 'run_microvm', **record['request'])
        sandbox = Sandbox(self, vm, token)
        try:
            await self.write(self.registry_key(name), {**record, 'vm_id': vm['microvmId']})
            await self.initialize(sandbox, record)
            return sandbox
        except BaseException:
            await asyncio.shield(sandbox.terminate.aio())
            raise

    async def initialize(self, sandbox, record):
        await sandbox.running()
        await sandbox.request('/bootstrap', {})
        async with asyncio.timeout(60):
            while True:
                try:
                    if (await sandbox.request('/health', {})).get('bootstrapped'):
                        break
                except (httpx.TransportError, RuntimeError):
                    pass  # Only readiness is retried; never command execution.
                await asyncio.sleep(.25)
        manifest = await self.checkpoint(record['snapshot_id']) if record['snapshot_id'] else None
        body = {'id': record['request']['clientToken'], 'apt_packages': record['apt_packages']}
        if manifest:
            body['restore'] = {**manifest, 'url': await self.url('get_object', manifest['key'])}
        await sandbox.job('/initialize', body, timeout=900)

    async def check(self):
        image = await self.aws('lambda-microvms', 'get_microvm_image_version',
            imageIdentifier=self.settings.lambda_image, imageVersion=self.settings.lambda_image_version)
        if image.get('state') != 'SUCCESSFUL':
            raise ValueError('The selected AWS MicroVM image version is not ready')
        await self.aws('s3', 'head_bucket', Bucket=self.settings.lambda_checkpoint_bucket)
        sandbox = await self.create(timeout=300)
        try:
            process = await sandbox.exec.aio('/opt/hermes-env/bin/python', '-c',
                'from run_agent import AIAgent; import mcp, claude_agent_sdk; print("moyai-connected")', timeout=30)
            out, _ = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
            if await process.wait.aio() or out.strip() != 'moyai-connected':
                raise RuntimeError('AWS sandbox command check failed')
        finally:
            await sandbox.terminate.aio()
        return 'Connected to AWS Lambda MicroVMs. A test VM ran successfully and termination was confirmed.'


class Sandbox:
    def __init__(self, provider, vm, token=''):
        self.provider, self.vm = provider, vm
        self.vm_id = vm['microvmId']
        self.object_id = 'lambda:' + provider.settings.lambda_region + ':' + self.vm_id
        started = vm['startedAt']
        self.started_at = started.timestamp() if hasattr(started, 'timestamp') else float(started)
        self.env = {'WORKSPACE_RUN_TOKEN': token} if token else {}
        self.filesystem = Filesystem(self)
        self.auth, self.auth_until = '', 0
        self.auth_lock = asyncio.Lock()
        self.lock = _LOCKS.setdefault(self.object_id, asyncio.Lock())

    async def running(self):
        async with asyncio.timeout(180):
            while True:
                self.vm = await self.provider.aws('lambda-microvms', 'get_microvm', microvmIdentifier=self.vm_id)
                if self.vm['state'] == 'RUNNING':
                    return
                if self.vm['state'] != 'PENDING':
                    raise RuntimeError('AWS VM is not running: ' + self.vm['state'])
                await asyncio.sleep(1)

    async def request(self, path, data):
        # Different handles (stream readers, children, Computer) share one VM.
        # Wait for checkpoints instead of failing their concurrent reads.
        async with self.lock:
            return await self._request(path, data)

    async def _request(self, path, data):
        async with self.auth_lock:
            if time.monotonic() >= self.auth_until:
                result = await self.provider.aws('lambda-microvms', 'create_microvm_auth_token',
                    microvmIdentifier=self.vm_id, expirationInMinutes=15, allowedPorts=[{'port': 80}])
                self.auth = result['authToken']['X-aws-proxy-auth']
                self.auth_until = time.monotonic() + 12 * 60
        endpoint = self.vm['endpoint']
        url = urlsplit(endpoint if '://' in endpoint else 'https://' + endpoint)
        if (url.scheme != 'https' or url.username or url.password or url.port or
                not url.hostname.endswith('.lambda-microvm.' + self.provider.settings.lambda_region + '.on.aws')
                or url.path not in {'', '/'} or url.query or url.fragment):
            raise ValueError('Unexpected AWS MicroVM endpoint')
        # Observations and releasing the same checkpoint can be retried. Execution,
        # checkpoint submission and Computer input may have taken effect even
        # when AWS loses the reply, so they always get exactly one attempt.
        attempts = 3 if path in {'/health', '/job', '/read', '/file/read', '/file/stat', '/checkpoint/finish'} else 1
        async with httpx.AsyncClient(timeout=530 if path == '/computer' else 60, follow_redirects=False) as client:
            for attempt in range(attempts):
                if attempt:
                    await asyncio.sleep(.5 * attempt)
                try:
                    async with client.stream('POST', 'https://' + url.netloc + path,
                            json=data, headers={'X-aws-proxy-auth': self.auth, 'X-aws-proxy-port': '80'}) as response:
                        if response.status_code in {429, 500, 502, 503, 504}:
                            if attempt + 1 < attempts:
                                continue
                            raise TransientEndpointError(f'AWS sandbox returned HTTP {response.status_code}')
                        if response.status_code == 404:
                            raise FileNotFoundError('AWS sandbox file or execution not found')
                        if response.status_code != 200:
                            raise RuntimeError(f'AWS sandbox returned HTTP {response.status_code}')
                        result = bytearray()
                        async for chunk in response.aiter_bytes():
                            result.extend(chunk)
                            if len(result) > 2 * 1024 * 1024:
                                raise ValueError('AWS sandbox response exceeds 2 MiB')
                        return json.loads(result)
                except httpx.TransportError:
                    if attempt + 1 == attempts:
                        raise

    async def job(self, path, body, *, timeout):
        # The guest journals before executing. An uncertain POST can be polled
        # by ID, but is never silently sent again by this client.
        async with asyncio.timeout(timeout):
            try:
                await self._request(path, body)
            except (TransientEndpointError, httpx.TransportError):
                pass  # Resolve a lost acknowledgement only by observing its ID.
            while True:
                try:
                    result = await self._request('/job', {'id': body['id']})
                except (TransientEndpointError, httpx.TransportError):
                    # Cold image IO can coincide with sustained gateway errors.
                    # Keep observing the original job within its existing budget.
                    await asyncio.sleep(2)
                    continue
                if result['state'] == 'failed':
                    raise RuntimeError('AWS guest operation failed: ' + result.get('error', 'unknown'))
                if result['state'] == 'done':
                    return result['result']
                await asyncio.sleep(1)

    async def computer_request(self, body):
        return await self.request('/computer', body)

    @operation
    async def poll(self):
        vm = await self.provider.aws('lambda-microvms', 'get_microvm', microvmIdentifier=self.vm_id)
        # TERMINATING is still live: cleanup must confirm TERMINATED.
        return 1 if vm['state'] == 'TERMINATED' else None

    @operation
    async def terminate(self):
        try:
            await self.provider.aws('lambda-microvms', 'terminate_microvm', microvmIdentifier=self.vm_id)
            await self.wait.aio()
        except NotFoundError:
            pass

    @operation
    async def wait(self, **kwargs):
        async with asyncio.timeout(120):
            while True:
                try:
                    vm = await self.provider.aws('lambda-microvms', 'get_microvm', microvmIdentifier=self.vm_id)
                except NotFoundError:
                    return 0
                if vm['state'] == 'TERMINATED':
                    return 0
                await asyncio.sleep(1)

    @operation
    async def exec(self, *command, timeout=None, env=None, **kwargs):
        identity = uuid4().hex
        await self.request('/start', {'id': identity, 'command': list(command),
            'env': {**self.env, **(env or {})}, 'timeout': timeout})
        return Process(self, identity)

    @operation
    async def snapshot_filesystem(self, timeout=180, ttl=None):
        async with self.lock:
            return await self.checkpoint(timeout)

    async def checkpoint(self, timeout):
        identity = uuid4().hex
        key = self.provider.key('checkpoints/' + identity)
        try:
            async with asyncio.timeout(timeout):
                meta = await self.job('/checkpoint', {'id': identity, 'timeout': timeout,
                    'max_bytes': MAX_ARCHIVE}, timeout=timeout)
                url = await self.provider.url('put_object', key + '.tar.gz',
                    ChecksumSHA256=meta['checksum'], ServerSideEncryption='AES256')
                await self.job('/upload', {'id': uuid4().hex, 'checkpoint': identity, 'url': url,
                    'checksum': meta['checksum']}, timeout=timeout)
                obj = await self.provider.aws('s3', 'head_object', Bucket=self.provider.settings.lambda_checkpoint_bucket,
                    Key=key + '.tar.gz', ChecksumMode='ENABLED')
                if obj['ContentLength'] != meta['size'] or obj.get('ChecksumSHA256') != meta['checksum']:
                    raise RuntimeError('S3 checkpoint verification failed')
                manifest = {'version': 1, 'key': key + '.tar.gz', 'sha256': meta['sha256'], 'size': meta['size'],
                    'image': self.vm['imageArn'], 'image_version': self.vm['imageVersion']}
                await self.provider.write(key + '.json', manifest, IfNoneMatch='*')
        finally:
            # The guest also has an expiry watchdog if this server disconnects.
            await asyncio.shield(self._request('/checkpoint/finish', {'id': identity}))
        return SimpleNamespace(object_id=':'.join(('lambda', self.provider.settings.lambda_region,
            self.provider.settings.lambda_checkpoint_bucket, key + '.json')))
