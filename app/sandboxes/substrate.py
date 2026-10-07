"""Substrate control-plane client and Modal-shaped asynchronous sandbox handles."""
import asyncio
import base64
from functools import partial
import json
import ssl
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

from grpclib.client import Channel, UnaryUnaryMethod
from grpclib.const import Status
from grpclib.exceptions import GRPCError
import httpx
from modal.exception import NotFoundError, AlreadyExistsError

from .proto import ateapi_pb2 as pb
from sandbox.substrate_protocol import VERSION, MAX_BODY, CHUNK, canonical, private_key

_ACTOR_LOCKS = WeakValueDictionary()


class GuestUnavailable(RuntimeError):
    pass


class operation:
    """Expose the async operation under the handle contract's .aio entry point."""
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        return SimpleNamespace(aio=partial(self.method, instance))


def ref(atespace, name):
    return {'atespace': atespace, 'name': name}


def tls_context(settings):
    context = ssl.create_default_context()
    if settings.substrate_ca_cert:
        context.load_verify_locations(cadata=settings.substrate_ca_cert)
    return context


class SubstrateProvider:
    name = 'substrate'

    def __init__(self, settings):
        self.settings = settings

    async def rpc(self, method, data, timeout=120):
        url = urlsplit(self.settings.substrate_api_url)
        tls = tls_context(self.settings) if url.scheme == 'https' else False
        if tls:
            tls.set_alpn_protocols(['h2'])
        channel = Channel(url.hostname, url.port or (443 if tls else 80), ssl=tls)
        descriptor = pb.DESCRIPTOR.services_by_name['Control'].methods_by_name[method]
        request_type = getattr(pb, descriptor.input_type.name)
        response_type = getattr(pb, descriptor.output_type.name)
        token = self.settings.substrate_api_token
        if self.settings.substrate_token_file:
            from pathlib import Path
            token = Path(self.settings.substrate_token_file).read_text().strip()
        try:
            return await UnaryUnaryMethod(channel, '/ateapi.Control/' + method, request_type, response_type)(
                request_type(**data), timeout=timeout, metadata={'authorization': 'Bearer ' + token} if token else {})
        except GRPCError as exc:
            if exc.status == Status.NOT_FOUND:
                raise NotFoundError('Substrate resource not found') from None
            if exc.status == Status.ALREADY_EXISTS:
                raise AlreadyExistsError('Substrate resource already exists') from None
            raise RuntimeError('Substrate control API returned ' + exc.status.name) from None
        finally:
            channel.close()

    async def get(self, identity):
        kind, space, name, uid = identity.split(':')
        if kind != 'substrate':
            raise ValueError('Invalid Substrate sandbox ID')
        actor = await self.rpc('GetActor', {'actor': ref(space, name)})
        if actor.metadata.uid != uid:
            raise NotFoundError('Substrate actor was replaced')
        return Sandbox(self, actor)

    async def find(self, name, *, initialize=False, token='', timeout=86400, apt_packages=()):
        actor = await self.rpc('GetActor', {'actor': ref(self.settings.substrate_atespace, name)})
        sandbox = Sandbox(self, actor, token=token)
        if initialize:
            await self.initialize(sandbox, timeout=timeout, apt_packages=apt_packages)
        return sandbox

    async def initialize(self, sandbox, *, timeout, apt_packages):
        # Safe to repeat after the control plane accepted CreateActor but the
        # provisioner lost its response. Finding an actor for cleanup is read-only.
        try:
            await self.rpc('CreateActorEgressPolicy', {
                'actor': sandbox.ref, 'egress_policy': {'metadata': ref(sandbox.ref['atespace'], 'default'), 'rules': [
                    {'http': {'hostnames': self.settings.substrate_egress_hosts.split(','), 'ports': {'numbers': [80]}}},
                    {'tls_passthrough': {'hostnames': self.settings.substrate_egress_hosts.split(','), 'ports': {'all': {}}}}]}})
        except AlreadyExistsError:
            pass
        await self.rpc('ResumeActor', {'actor': sandbox.ref})
        await sandbox.request('/activate', {'expires_at': time.time() + timeout})
        if apt_packages:
            process = await sandbox.exec.aio('sh', '-c', 'apt-get update && apt-get install -y --no-install-recommends "$@"',
                                             'moyai-apt', *apt_packages, timeout=900)
            await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
            if await process.wait.aio():
                raise RuntimeError('Substrate environment package installation failed')

    async def create(self, *, name=None, snapshot_id='', token='', timeout=86400, memory=4096, apt_packages=()):
        space, template = self.settings.substrate_atespace, self.settings.substrate_template
        actor_data = {'metadata': ref(space, name or 'moyai-' + uuid4().hex), 'actor_template': ref(space, template)}
        if snapshot_id:
            kind, tag_space, template, tag = snapshot_id.split(':')
            if kind != 'substrate' or tag_space != space:
                raise ValueError('Snapshot belongs to another sandbox provider or Substrate atespace')
            actor_data.update(actor_template=ref(space, template), source_tag=ref(space, tag))
        owned = True
        try:
            actor = await self.rpc('CreateActor', {'actor': actor_data})
        except AlreadyExistsError:
            owned = False
            actor = await self.rpc('GetActor', {'actor': actor_data['metadata']})
        sandbox = Sandbox(self, actor, token=token)
        try:
            await self.initialize(sandbox, timeout=timeout, apt_packages=apt_packages)
            return sandbox
        except BaseException:
            if owned:
                await asyncio.shield(sandbox.terminate.aio())
            raise

    async def check(self):
        template = await self.rpc('GetActorTemplate', {'actor_template': ref(self.settings.substrate_atespace, self.settings.substrate_template)})
        if (template.snapshot_config.on_commit != pb.SNAPSHOT_CONTENT_SCOPE_FULL or
                any(v.HasField('external_volume_template') for v in template.volumes)):
            raise ValueError('Use the Moyai FULL-snapshot template without external volumes.')
        # Exercise routing, the template image, and the signing key, then release
        # compute even when validation fails. This is more than a credential check.
        sandbox = await self.create(name='moyai-check-' + uuid4().hex, timeout=120)
        try:
            response = await sandbox.request('/activate', {})
            if response.get('version') != VERSION:
                raise ValueError('Update the Moyai Substrate runtime image.')
            process = await sandbox.exec.aio('/opt/hermes-env/bin/python', '-c',
                'from run_agent import AIAgent; import mcp, claude_agent_sdk; print("moyai-connected")', timeout=30)
            out, _ = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
            if await process.wait.aio() or out.strip() != 'moyai-connected':
                raise RuntimeError('The sandbox could not execute a command.')
        finally:
            await sandbox.terminate.aio()
        return 'Connected to Substrate. A test sandbox ran successfully and was deleted.'


class Sandbox:
    def __init__(self, provider, actor, token=''):
        self.provider, self.actor = provider, actor
        self.ref = ref(actor.metadata.atespace, actor.metadata.name)
        self.object_id = ':'.join(('substrate', actor.metadata.atespace, actor.metadata.name, actor.metadata.uid))
        self.lock = _ACTOR_LOCKS.setdefault(self.object_id, asyncio.Lock())
        self.env = {'WORKSPACE_RUN_TOKEN': token} if token else {}
        self.filesystem = Filesystem(self)

    async def request(self, path, data):
        # Stream readers and Computer can use different handles for one actor.
        # Hold their requests while a checkpoint freezes the guest processes.
        async with self.lock:
            return await self._request(path, data)

    async def computer_request(self, body):
        """One signed desktop call, without an exec journal or mutation retry."""
        return await self.request('/computer', body)

    async def _request(self, path, data):
        if path != '/activate':
            return await self._request_once(path, data)
        # Clones exec their guest service before accepting commands to discard
        # parent credentials from memory. Its in-flight activation disconnects.
        async with asyncio.timeout(90):
            for attempt in range(20):
                try:
                    return await self._request_once(path, data)
                except (httpx.TransportError, GuestUnavailable):
                    if attempt == 19:
                        raise
                    await asyncio.sleep(.5)

    async def _request_once(self, path, data):
        body = json.dumps(data, separators=(',', ':')).encode()
        stamp, nonce = str(int(time.time())), uuid4().hex
        signature = private_key(self.provider.settings.substrate_signing_key).sign(
            canonical(self.actor.metadata.uid, stamp, nonce, path, body))
        headers = {'ate-target-actor': self.ref['atespace'] + '/' + self.ref['name'],
                   'X-Moyai-Actor': self.actor.metadata.uid, 'X-Moyai-Time': stamp, 'X-Moyai-Nonce': nonce,
                   'X-Moyai-Signature': base64.b64encode(signature).decode(), 'Content-Type': 'application/json'}
        settings = self.provider.settings
        verify = tls_context(settings)
        async with httpx.AsyncClient(timeout=530 if path == '/computer' else 60,
                                     verify=verify, follow_redirects=False) as client:
            async with client.stream('POST', settings.substrate_router_url.rstrip('/') + path, content=body, headers=headers) as response:
                if path == '/computer' and response.status_code in {404, 409}:
                    raise RuntimeError('Computer could not confirm the request. Restart this workspace to update or '
                                       'reconnect it, and check the desktop before repeating input.')
                if response.status_code == 404:
                    raise FileNotFoundError('Sandbox file or execution not found')
                if response.status_code in {502, 503, 504}:
                    raise GuestUnavailable(f'Substrate sandbox returned HTTP {response.status_code}')
                if response.status_code != 200:
                    raise RuntimeError(f'Substrate sandbox returned HTTP {response.status_code}')
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > MAX_BODY:
                        raise ValueError('Sandbox response exceeds 2 MiB')
                return json.loads(result)

    @operation
    async def poll(self):
        actor = await self.provider.rpc('GetActor', {'actor': self.ref})
        return None if actor.status.state in {pb.ACTOR_STATE_RUNNING, pb.ACTOR_STATE_SUSPENDED, pb.ACTOR_STATE_PAUSED} else 1

    @operation
    async def terminate(self):
        try:
            await self.provider.rpc('DeleteActor', {'actor': self.ref, 'any_state': True,
                                                   'options': {'uid': self.actor.metadata.uid}})
        except NotFoundError:
            pass

    @operation
    async def wait(self, **kwargs):
        return 0  # DeleteActor only returns after terminating the workload.

    @operation
    async def exec(self, *command, timeout=None, env=None, **kwargs):
        identity = uuid4().hex
        await self.request('/start', {'id': identity, 'command': list(command), 'env': {**self.env, **(env or {})}, 'timeout': timeout})
        return Process(self, identity)

    @operation
    async def snapshot_filesystem(self, timeout=180, ttl=None):
        tag = 'moyai-' + uuid4().hex
        async with self.lock:
            try:
                await self._request('/freeze', {})
                await self.provider.rpc('SuspendActor', {'actor': self.ref}, timeout=timeout)
                await self.provider.rpc('CreateTag', {'tag': {'metadata': ref(self.ref['atespace'], tag),
                                                            'scope': pb.TAG_SCOPE_ATESPACE, 'source_actor': self.ref}}, timeout=timeout)
            finally:
                await self.provider.rpc('ResumeActor', {'actor': self.ref}, timeout=timeout)
                await self._request('/activate', {})
        return SimpleNamespace(object_id=':'.join(('substrate', self.ref['atespace'], self.actor.actor_template.name, tag)))


class Filesystem:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    @operation
    async def write_text(self, text, path):
        data = text.encode()
        for offset in range(0, max(1, len(data)), CHUNK):
            await self.sandbox.request('/file/write', {'path': path, 'offset': offset,
                'data': base64.b64encode(data[offset:offset + CHUNK]).decode()})

    @operation
    async def stat(self, path):
        return SimpleNamespace(**await self.sandbox.request('/file/stat', {'path': path}))

    @operation
    async def read_bytes(self, path):
        result = bytearray()
        while True:
            value = await self.sandbox.request('/file/read', {'path': path, 'offset': len(result)})
            data = base64.b64decode(value['data'], validate=True)
            result.extend(data)
            if len(result) > 24 * 1024 * 1024:
                raise ValueError('Sandbox download exceeds 24 MiB')
            if not data:
                return bytes(result)


class Process:
    def __init__(self, sandbox, identity):
        self.sandbox, self.identity = sandbox, identity
        self.stdout, self.stderr = Stream(self, 'stdout'), Stream(self, 'stderr')
        self.exit_code = None
        self.lock = asyncio.Lock()

    async def read(self):
        async with self.lock:
            result = await self.sandbox.request('/read', {'id': self.identity, 'stdout': self.stdout.offset, 'stderr': self.stderr.offset})
            for stream in (self.stdout, self.stderr):
                data = base64.b64decode(result[stream.name])
                stream.offset += len(data)
                stream.buffer.extend(data)
            self.exit_code = result['exit_code']
            # A finished process can still have more than one chunk buffered.
            self.drained = self.exit_code is not None and not result['stdout'] and not result['stderr']

    @operation
    async def wait(self):
        while self.exit_code is None:
            await self.read()
            await asyncio.sleep(.1)
        return self.exit_code


class Stream:
    def __init__(self, process, name):
        self.process, self.name = process, name
        self.buffer, self.offset = bytearray(), 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                return (line + b'\n').decode(errors='replace')
            if getattr(self.process, 'drained', False):
                if self.buffer:
                    data, self.buffer = self.buffer, bytearray()
                    return data.decode(errors='replace')
                raise StopAsyncIteration
            await self.process.read()
            if not self.buffer:
                await asyncio.sleep(.15)

    @operation
    async def read(self):
        return ''.join([chunk async for chunk in self])
