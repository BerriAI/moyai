"""One AWS command's disposable, memory-backed home and cache collection."""
import json
import os
from pathlib import Path
import stat
import tempfile

from sandbox.aws_credentials import MAX_BUNDLE, bundle, cache_paths, configuration


class AWSHome:
    def __init__(self, value: str):
        self.original = bundle(value)
        self.temporary = None

    def __enter__(self):
        # Never fall back to a snapshotted workspace or the agent's real HOME.
        mounts = Path('/proc/mounts').read_text().splitlines()
        if not any(line.split()[1:3] == ['/dev/shm', 'tmpfs'] for line in mounts):
            raise ValueError('AWS login commands require a Linux tmpfs at /dev/shm.')
        self.temporary = tempfile.TemporaryDirectory(prefix='moyai-aws-', dir='/dev/shm')
        self.home = Path(self.temporary.name)
        try:
            root = self.home / '.aws'
            for path in ('', 'sso', 'sso/cache', 'cli', 'cli/cache'):
                (root / path).mkdir(mode=0o700, exist_ok=True)
            files = {'config': configuration(self.original['profile']), 'credentials': '',
                     **{path: json.dumps(value) for path, value in self.original['cache'].items()}}
            for path, value in files.items():
                destination = root / path
                fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(value)
            return self
        except BaseException:
            self.temporary.cleanup()
            raise

    def environment(self) -> dict[str, str]:
        return {'HOME': str(self.home), 'AWS_CONFIG_FILE': str(self.home / '.aws/config'),
                'AWS_SHARED_CREDENTIALS_FILE': str(self.home / '.aws/credentials'),
                'AWS_PROFILE': self.original['profile']['profile'], 'AWS_EC2_METADATA_DISABLED': 'true',
                'AWS_CLI_AUTO_PROMPT': 'off', 'AWS_PAGER': '', 'AWS_MAX_ATTEMPTS': '1',
                'BOTO_CONFIG': '/dev/null'}

    def collect(self) -> dict:
        cache = {}
        for relative in cache_paths(self.original['profile']):
            # Open every component without following links, including parents.
            # Nonblocking open prevents a command-created FIFO from hanging us.
            descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = ('.aws/' + relative).split('/')
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                try:
                    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BUNDLE:
                        raise ValueError('Unsafe AWS cache file.')
                    raw = stream.read(MAX_BUNDLE + 1)
                    if len(raw) > MAX_BUNDLE:
                        raise ValueError('AWS cache exceeded limit.')
                    cache[relative] = json.loads(raw)
            finally:
                os.close(descriptor)
        return bundle(json.dumps({**self.original, 'cache': cache}))

    def __exit__(self, *args):
        self.temporary.cleanup()
