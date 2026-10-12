"""Refresh only changed runner files over the common sandbox transport."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import zlib

RUNTIME_ROOT = '/opt/workspace-runner'
SANDBOX_PYTHON = '/usr/local/bin/python'

# Live supervisors and desktops can predate their next runtime refresh. Only
# these controller-owned entrypoints may fall back to the old flat layout.
RUNTIME_COMMAND = """
import pathlib, runpy, sys
root = pathlib.Path(sys.argv.pop(1))
name = sys.argv.pop(1)
if name not in {'durable_process.py', 'computer.py', 'environment_build.py'}:
    raise RuntimeError('Invalid runtime command')
script = root / 'sandbox' / name
if not script.is_file():
    script = root / name
sys.path.insert(0, str(root))
sys.argv[0] = str(script)
if script.parent == root:
    runpy.run_path(str(script), run_name='__main__')
else:
    runpy.run_module('sandbox.' + script.stem, run_name='__main__')
"""

# Sent by the controller: old snapshots need no installed helper or version
# marker. Isolated Python also ignores modules planted in the workspace.
SYNC_SCRIPT = r'''
import base64, contextlib, fcntl, hashlib, json, os, pathlib, stat, sys, tempfile, zlib
root = pathlib.Path(sys.argv[1])
expected = json.loads(sys.argv[2])
bundle = pathlib.Path(sys.argv[3]) if sys.argv[3] else None
if not isinstance(expected, dict) or root.is_symlink():
    raise RuntimeError('Invalid runtime path')
for name in expected:
    path = pathlib.PurePosixPath(name)
    if name in ('', '.', '..') or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        raise RuntimeError('Invalid runtime path')
root.mkdir(parents=True, exist_ok=True)
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

@contextlib.contextmanager
def parent_directory(name, create=False):
    directory = os.dup(root_fd)
    try:
        for part in pathlib.PurePosixPath(name).parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=directory)
                except FileExistsError:
                    pass
            # Opening each component separately rejects symlink parents, even
            # when one is swapped after the manifest was checked.
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)

def changed():
    result = []
    for name, digest in expected.items():
        try:
            with parent_directory(name) as directory:
                leaf = pathlib.PurePosixPath(name).name
                info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    result.append(name)
                    continue
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise RuntimeError('Invalid runtime file')
                    if hashlib.sha256(source.read()).hexdigest() != digest:
                        result.append(name)
        except FileNotFoundError:
            result.append(name)
    return result

try:
    fd = os.open('.runtime-update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    with os.fdopen(fd, 'a') as lock:
        # A cancelled controller can leave its bounded installer finishing.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if bundle:
            files = json.loads(zlib.decompress(base64.b64decode(bundle.read_bytes(), validate=True)))
            if not isinstance(files, dict) or not files or not files.keys() <= expected.keys():
                raise RuntimeError('Invalid runtime bundle')
            with tempfile.TemporaryDirectory(prefix='.runtime-update-', dir=root) as temporary:
                for index, (name, text) in enumerate(files.items()):
                    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != expected[name]:
                        raise RuntimeError('Invalid runtime content')
                    pathlib.Path(temporary, str(index)).write_bytes(text.encode())
                for index, name in enumerate(files):
                    with parent_directory(name, create=True) as directory:
                        leaf = pathlib.PurePosixPath(name).name
                        os.replace(pathlib.Path(temporary, str(index)), leaf, dst_dir_fd=directory)
                        # Same-second, same-size updates must not reuse old bytecode.
                        if name.endswith('.py'):
                            try:
                                cache = os.open('__pycache__', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                dir_fd=directory)
                            except FileNotFoundError:
                                continue
                            try:
                                prefix = pathlib.PurePosixPath(name).stem + '.'
                                for compiled in os.listdir(cache):
                                    if compiled.startswith(prefix) and compiled.endswith('.pyc'):
                                        os.unlink(compiled, dir_fd=cache)
                            finally:
                                os.close(cache)
            if changed():
                raise RuntimeError('Runtime changed during installation')
        print(json.dumps(changed()))
finally:
    os.close(root_fd)
    if bundle:
        bundle.unlink(missing_ok=True)
'''


async def _sync_command(sandbox, expected: dict[str, str], bundle: str = '') -> list[str]:
    process = await sandbox.exec.aio(SANDBOX_PYTHON, '-I', '-c', SYNC_SCRIPT,
                                     RUNTIME_ROOT, json.dumps(expected), bundle, timeout=30)
    output, _ = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
    if await process.wait.aio() != 0:
        raise RuntimeError('Workspace runtime verification failed')
    try:
        changed = json.loads(output)
    except (TypeError, ValueError):
        raise RuntimeError('Invalid workspace runtime verification') from None
    if (not isinstance(changed, list) or any(not isinstance(name, str) or name not in expected for name in changed)
            or len(changed) != len(set(changed))):
        raise RuntimeError('Invalid workspace runtime verification')
    return changed


def runtime_sources(source: Path) -> list[Path]:
    """The complete guest runtime, shared by refreshes and prepared-pool identity."""
    return [path for package in ('agent', 'sandbox')
            for path in sorted((source / package).rglob('*'))
            if path.is_file() and not path.is_symlink()
            and path.suffix in {'.py', '.md', '.patch'} and '__pycache__' not in path.parts]


async def sync_runtime(sandbox, source: Path) -> None:
    files = {path.relative_to(source).as_posix(): path.read_text(encoding='utf-8')
             for path in runtime_sources(source)}
    expected = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
    async with asyncio.timeout(60):
        changed = await _sync_command(sandbox, expected)
        if not changed:
            return
        # write_text is shared by Modal, Substrate and Lambda. Compression keeps
        # this one upload; putting the bundle in argv exceeds Linux's arg limit.
        data = json.dumps({name: files[name] for name in changed}, ensure_ascii=False).encode()
        bundle = '/tmp/moyai-runtime-' + uuid4().hex + '.bundle'
        await sandbox.filesystem.write_text.aio(base64.b64encode(zlib.compress(data)).decode(), bundle)
        if await _sync_command(sandbox, expected, bundle):
            raise RuntimeError('Workspace runtime installation incomplete')
