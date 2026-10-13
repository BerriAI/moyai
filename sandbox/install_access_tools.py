"""Install pinned upstream CLIs in new images and older restored sandboxes."""
import hashlib
import fcntl
import io
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile


TOOLS = {'aws', 'kubectl', 'helm', 'op'}
INSTALL_DIR = Path('/usr/local/bin')
# Official AWS Linux bundles. Both release signatures were verified against
# AWS's published key FB5DB77FD5C118B80511ADA8A6310ACC4672475C before pinning.
AWS_VERSION = '2.38.0'
AWS_SHA256 = {
    'amd64': 'cb037d35cca5cefc3eb89ebabc471a0b4e4fe44135b72228c7eb0da2d2fae9f6',
    'arm64': '4987b0d4e12019b8decefcd826393dd3c71ea72c87b88b3aeca570b7039a54c0',
}
AWS_INSTALL_DIR = Path('/usr/local/aws-cli')
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


def aws_current() -> bool:
    executable = shutil.which('aws')
    if not executable:
        return False
    try:
        result = subprocess.run([executable, '--version'], capture_output=True, timeout=10)
        return result.returncode == 0 and result.stdout.startswith(f'aws-cli/{AWS_VERSION} '.encode())
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_aws(architecture: str) -> None:
    # Restored v1 images also need upgrading. Serialize concurrent tool calls
    # before replacing the shared install, and recheck after acquiring the lock.
    with (INSTALL_DIR / '.moyai-aws-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if aws_current():
            return
        machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}[architecture]
        content = download(f'https://awscli.amazonaws.com/awscli-exe-linux-{machine}-{AWS_VERSION}.zip')
        if hashlib.sha256(content).hexdigest() != AWS_SHA256[architecture]:
            raise ValueError('AWS CLI checksum mismatch')
        # v1 may have been installed by the restored agent's virtualenv Python.
        # Upgrade the executable actually selected by PATH, not a shadowed bin.
        existing = shutil.which('aws')
        bin_dir = Path(existing).absolute().parent if existing else INSTALL_DIR
        with tempfile.TemporaryDirectory(prefix='moyai-aws-install-') as directory:
            root = Path(directory)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
                    raise ValueError('AWS CLI archive exceeded limit')
                for item in archive.infolist():
                    path = Path(item.filename)
                    if path.is_absolute() or '..' in path.parts or path.parts[0] != 'aws':
                        raise ValueError('Invalid AWS CLI archive path')
                    target = Path(archive.extract(item, root))
                    if not item.is_dir():
                        target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
            subprocess.run(['/bin/sh', str(root / 'aws/install'), '--install-dir', str(AWS_INSTALL_DIR),
                            '--bin-dir', str(bin_dir), '--update'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
        # AWS's installer skips an already installed version, including its
        # symlink setup. Repair a v1 virtualenv entry shadowing that v2 install.
        if not aws_current():
            installed = AWS_INSTALL_DIR / 'v2' / AWS_VERSION / 'bin/aws'
            if installed.is_file():
                link = bin_dir / f'.moyai-aws-{os.getpid()}'
                try:
                    link.symlink_to(installed)
                    link.replace(bin_dir / 'aws')
                finally:
                    link.unlink(missing_ok=True)
        if not aws_current():
            raise RuntimeError('AWS CLI v2 installation could not be verified')


def ensure_tools(command: str = '') -> None:
    needed = TOOLS if not command else {
        Path(token).name for token in shlex.split(command) if Path(token).name in TOOLS}
    missing = {name for name in needed if not shutil.which(name)}
    if 'aws' in needed and not aws_current():
        missing.add('aws')
    if not missing:
        return
    architecture = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or not architecture:
        raise RuntimeError('Infrastructure tools require a Linux sandbox')
    if 'aws' in missing:
        install_aws(architecture)
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
