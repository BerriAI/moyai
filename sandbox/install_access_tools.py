"""Install pinned upstream CLIs in new images and older restored sandboxes."""
import hashlib
import io
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


TOOLS = {'aws', 'kubectl', 'helm', 'op'}
INSTALL_DIR = Path('/usr/local/bin')
# Official 1Password Linux release archives; pin both version and content.
OP_VERSION = '2.30.0'
OP_SHA256 = {
    'amd64': 'cd5361b074cd40eb2b332885f35a4d61c74369919ced95190c885f4d4f739dc7',
    'arm64': 'c0618a4d4defa5d61606dfb4eaf7d5f39cf6361382c4943449df95fa1f7cc310',
}


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise ValueError('CLI download exceeded limit')
    return data


def ensure_tools(command: str = '') -> None:
    needed = TOOLS if not command else {
        Path(token).name for token in shlex.split(command) if Path(token).name in TOOLS}
    missing = {name for name in needed if not shutil.which(name)}
    if not missing:
        return
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or not architecture:
        raise RuntimeError('Infrastructure tools require a Linux sandbox')
    if 'aws' in missing:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', 'awscli==1.42.30'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
    for name in sorted(missing - {'aws'}):
        url = (f'https://cache.agilebits.com/dist/1P/op2/pkg/v{OP_VERSION}/op_linux_{architecture}_v{OP_VERSION}.zip' if name == 'op'
               else f'https://dl.k8s.io/release/v1.34.1/bin/linux/{architecture}/kubectl' if name == 'kubectl'
               else f'https://get.helm.sh/helm-v3.19.0-linux-{architecture}.tar.gz')
        content = download(url)
        checksum = (OP_SHA256[architecture] if name == 'op'
                    else download(url + ('.sha256' if name == 'kubectl' else '.sha256sum')).decode().split()[0])
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError('CLI checksum mismatch')
        if name == 'helm':
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
                content = archive.extractfile(f'linux-{architecture}/helm').read()
        elif name == 'op':
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                content = archive.read('op')
        destination = INSTALL_DIR / name
        temporary = destination.with_name(name + f'.{os.getpid()}.tmp')
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o755)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    ensure_tools()
