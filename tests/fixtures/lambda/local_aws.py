"""Local AWS adapter for conformance tests. Compute is Docker; S3 is a TLS fixture.

This is deliberately NOT evidence of AWS IAM/network/control-plane compatibility.
The production provider, guest HTTP, archives, checksums and subprocesses are real.
"""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import quote
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
from modal.exception import NotFoundError

from app.sandboxes.lambda_microvm import LambdaProvider


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class LocalAWS:
    def __init__(self, settings, image):
        self.settings, self.image = settings, image
        self.directory = tempfile.TemporaryDirectory(prefix='moyai-lambda-')
        self.root = Path(self.directory.name)
        self.objects, self.urls, self.machines, self.tokens = {}, {}, {}, {}
        self._tls()
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_PUT(self):
                grant = parent.urls.get(self.path)
                if not grant or grant['method'] != 'put_object':
                    self.send_error(403); return
                path = parent.root / uuid4().hex
                sha = hashlib.sha256()
                remaining = int(self.headers['Content-Length'])
                with path.open('wb') as out:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError('Incomplete upload')
                        sha.update(chunk); out.write(chunk); remaining -= len(chunk)
                checksum = base64.b64encode(sha.digest()).decode()
                if checksum != grant['ChecksumSHA256'] or self.headers.get('x-amz-checksum-sha256') != checksum:
                    self.send_error(400); return
                if self.headers.get('x-amz-server-side-encryption') != 'AES256':
                    self.send_error(400); return
                parent.objects[grant['Key']] = (path, checksum)
                self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers()
            def do_GET(self):
                grant = parent.urls.get(self.path)
                if not grant or grant['method'] != 'get_object':
                    self.send_error(403); return
                path, _ = parent.objects[grant['Key']]
                self.send_response(200); self.send_header('Content-Length', str(path.stat().st_size)); self.end_headers()
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        self.wfile.write(chunk)
        self.server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.root / 'certificate.pem'), str(self.root / 'key.pem'))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.backend = LambdaProvider(settings)
        self.backend.aws = self.call
        self.transport = httpx.AsyncHTTPTransport()

    def _tls(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Moyai local S3 fixture')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('host.docker.internal')]), critical=False)
            .sign(key, hashes.SHA256()))
        (self.root / 'certificate.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (self.root / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    async def call(self, service, method, **kw):
        if method == 'run_microvm':
            if kw['clientToken'] in self.tokens:
                return self.machines[self.tokens[kw['clientToken']]]['vm']
            assert kw['idlePolicy']['maxIdleDurationSeconds'] >= kw['maximumDurationInSeconds']
            name = 'moyai-lambda-test-' + uuid4().hex[:12]
            container = await asyncio.to_thread(docker, 'run', '-d', '--name', name,
                '--add-host=host.docker.internal:host-gateway', '-p', '127.0.0.1::80',
                '-v', str(self.root / 'certificate.pem') + ':/run/test-ca.pem:ro',
                '-e', 'SSL_CERT_FILE=/run/test-ca.pem', self.image)
            port = (await asyncio.to_thread(docker, 'port', container, '80/tcp')).rsplit(':', 1)[1]
            import time
            value = {'microvmId': name, 'state': 'RUNNING', 'startedAt': time.time(),
                     'endpoint': name + '.lambda-microvm.us-east-1.on.aws', 'imageArn': self.settings.lambda_image,
                     'imageVersion': self.settings.lambda_image_version, 'maximumDurationInSeconds': kw['maximumDurationInSeconds']}
            self.machines[name] = {'vm': value, 'container': container, 'port': port}
            self.tokens[kw['clientToken']] = name
            # Read-only readiness retry while the container starts.
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        if (await client.post('http://127.0.0.1:' + port + '/aws/lambda-microvms/runtime/v1/ready')).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError('Local guest did not start')
            return value
        if method == 'get_microvm':
            return self.machines[kw['microvmIdentifier']]['vm']
        if method == 'terminate_microvm':
            machine = self.machines[kw['microvmIdentifier']]
            if machine['vm']['state'] != 'TERMINATED':
                await asyncio.to_thread(docker, 'rm', '-f', machine['container'])
                machine['vm']['state'] = 'TERMINATED'
            return {}
        if method == 'create_microvm_auth_token':
            assert kw['allowedPorts'] == [{'port': 80}]
            return {'authToken': {'X-aws-proxy-auth': 'local-endpoint-token'}}
        if method == 'put_object':
            if kw.get('IfNoneMatch') == '*' and kw['Key'] in self.objects:
                raise RuntimeError('AWS put_object failed: PreconditionFailed')
            path = self.root / uuid4().hex
            path.write_bytes(kw['Body'])
            self.objects[kw['Key']] = (path, '')
            return {}
        if method == 'get_object':
            if kw['Key'] not in self.objects:
                raise NotFoundError('Object missing')
            return {'Body': io.BytesIO(self.objects[kw['Key']][0].read_bytes())}
        if method == 'head_object':
            path, checksum = self.objects[kw['Key']]
            return {'ContentLength': path.stat().st_size, 'ChecksumSHA256': checksum}
        if method == 'generate_presigned_url':
            path = '/' + uuid4().hex + '/' + quote(kw['Params']['Key'], safe='')
            self.urls[path] = {**kw['Params'], 'method': kw['ClientMethod']}
            return f'https://host.docker.internal:{self.server.server_port}' + path
        if method == 'delete_object':
            obj = self.objects.pop(kw['Key'], None)
            if obj:
                obj[0].unlink(missing_ok=True)
            return {}
        raise AssertionError((service, method))

    async def map_request(self, request):
        name = request.url.host.split('.')[0]
        if name not in self.machines:
            # Local readiness requests also pass through the patched client.
            return await self.transport.handle_async_request(request)
        assert request.headers['X-aws-proxy-auth'] == 'local-endpoint-token'
        assert request.headers['X-aws-proxy-port'] == '80'
        machine = self.machines[name]
        request.url = request.url.copy_with(scheme='http', host='127.0.0.1', port=int(machine['port']))
        return await self.transport.handle_async_request(request)

    async def close(self):
        for machine in self.machines.values():
            if machine['vm']['state'] != 'TERMINATED':
                await asyncio.to_thread(docker, 'rm', '-f', machine['container'])
        await self.transport.aclose()
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()
