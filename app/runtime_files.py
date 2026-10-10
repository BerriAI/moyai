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

# Sent by the controller: old snapshots need no installed helper or version
# marker. Isolated Python also ignores modules planted in the workspace.
SYNC_SCRIPT = r'''
import base64, fcntl, hashlib, json, os, pathlib, sys, tempfile, zlib
root = pathlib.Path(sys.argv[1])
expected = json.loads(sys.argv[2])
bundle = pathlib.Path(sys.argv[3]) if sys.argv[3] else None
if root.is_symlink() or any(pathlib.Path(n).name != n or n in ('.', '..') for n in expected):
    raise RuntimeError('Invalid runtime path')
root.mkdir(parents=True, exist_ok=True)
def changed():
    return [n for n, digest in expected.items() if (root / n).is_symlink()
            or not (root / n).is_file()
            or hashlib.sha256((root / n).read_bytes()).hexdigest() != digest]
try:
    fd = os.open(root / '.runtime-update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        # A cancelled controller can leave its bounded installer finishing.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if bundle:
            files = json.loads(zlib.decompress(base64.b64decode(bundle.read_bytes(), validate=True)))
            if not isinstance(files, dict) or not files or not files.keys() <= expected.keys():
                raise RuntimeError('Invalid runtime bundle')
            with tempfile.TemporaryDirectory(prefix='.runtime-update-', dir=root) as temporary:
                for name, text in files.items():
                    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != expected[name]:
                        raise RuntimeError('Invalid runtime content')
                    pathlib.Path(temporary, name).write_bytes(text.encode())
                for name in files:
                    os.replace(pathlib.Path(temporary, name), root / name)
                    # Same-second, same-size updates must not reuse old bytecode.
                    cache = root / '__pycache__'
                    if name.endswith('.py') and cache.is_dir() and not cache.is_symlink():
                        for compiled in cache.glob(pathlib.Path(name).stem + '.*.pyc'):
                            compiled.unlink(missing_ok=True)
            if changed():
                raise RuntimeError('Runtime changed during installation')
        print(json.dumps(changed()))
finally:
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


async def sync_runtime(sandbox, source: Path) -> None:
    files = {path.name: path.read_text(encoding='utf-8') for path in
             sorted([*source.glob('*.py'), *source.glob('hermes-*.patch')])}
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
