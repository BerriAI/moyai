"""File-only MicroVM checkpoints. This module runs in the guest, never the web host."""
import base64
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
import os
import io
from itertools import islice
from pathlib import Path
import stat
import subprocess
import tarfile
import threading

# Runtime machinery, kernel mounts, host-injected networking, and per-exec
# credentials do not belong to another VM. Everything else is included, even
# ignored/untracked files, package databases, home directories, and /session.
EXCLUDED = ('proc', 'sys', 'dev', 'run', 'var/lib/moyai-runtime', 'opt/moyai-lambda',
            'etc/hostname', 'etc/hosts', 'etc/resolv.conf', '.dockerenv')
RUNTIME = Path('/var/lib/moyai-runtime')
BASELINE = Path('/opt/moyai-lambda/base.json')


def excluded(name):
    return any(name == item or name.startswith(item + '/') for item in EXCLUDED)


def inventory(root=Path('/')):
    names = []
    def walk(directory):
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if excluded(name):
                continue
            mode = path.lstat().st_mode
            # Sockets/devices are process/kernel state, not portable files.
            if stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                continue
            names.append(name)
            if stat.S_ISDIR(mode):
                walk(path)
    walk(root)
    return names


def attributes(path):
    return {key: base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode()
            for key in (os.listxattr(path, follow_symlinks=False) if hasattr(os, 'listxattr') else [])}


def signature(path, hash_file=None):
    info = path.lstat()
    content = ''
    if stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        content = (hash_file(path, info) if hash_file else digest(path)).hexdigest()
    return [info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            # Container/image layers normalize file timestamps to whole seconds.
            content, attributes(path), int(info.st_mtime) if not stat.S_ISDIR(info.st_mode) else 0]


def baseline(root=Path('/'), target=BASELINE):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({name: signature(root / name) for name in inventory(root)}))


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        if size < 16 * 1024 * 1024:
            while chunk := stream.read(1024 * 1024):
                value.update(chunk)
        else:
            # Large executables dominate cold AWS image reads. Fetch bounded
            # ranges concurrently, then hash their bytes in the original order.
            # Four 4 MiB ranges per file bound memory even with eight files in
            # flight. The caller quiesces commands before scanning the image.
            chunk_size = 4 * 1024 * 1024
            def read(offset):
                remaining = min(chunk_size, size - offset)
                chunks = []
                while remaining:
                    chunk = os.pread(stream.fileno(), remaining, offset)
                    if not chunk:
                        raise RuntimeError('File changed while hashing checkpoint')
                    chunks.append(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)
                return b''.join(chunks)
            offsets = iter(range(0, size, chunk_size))
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending = deque(pool.submit(read, offset) for offset in islice(offsets, 4))
                while pending:
                    value.update(pending.popleft().result())
                    offset = next(offsets, None)
                    if offset is not None:
                        pending.append(pool.submit(read, offset))
            if os.fstat(stream.fileno()).st_size != size:
                raise RuntimeError('File changed while hashing checkpoint')
    return value


def pack(destination, *, root=Path('/'), baseline_path=BASELINE, max_bytes):
    names = inventory(root)
    previous = json.loads(baseline_path.read_text())
    replaced = {name for name in names if name in previous and
                stat.S_IFMT(previous[name][0]) != stat.S_IFMT((root / name).lstat().st_mode)}
    deleted = sorted((set(previous) - set(names)) | replaced, key=lambda n: n.count('/'), reverse=True)
    # An independent delta against the immutable image, never another runtime
    # checkpoint. Hash contents so restored dependencies cannot be overlooked
    # merely because their size/mtime matches the image.
    # AWS restores image blocks on demand. Serial reads of the full image can
    # exhaust the checkpoint deadline even when very few files changed. Keep
    # full content hashing, with bounded parallel reads and deterministic order.
    changed = []
    hashes, hashes_lock = {}, threading.Lock()
    def hash_file(path, info):
        if info.st_nlink == 1:
            return digest(path)
        # Installed packages can share data with package-manager caches. The
        # guest is quiesced, so each inode needs one full read per checkpoint.
        # Never reuse this cache across checkpoints or restored VMs.
        key = (info.st_dev, info.st_ino)
        with hashes_lock:
            pending = hashes.get(key)
            owner = pending is None
            if owner:
                pending = hashes[key] = Future()
        if owner:
            try:
                pending.set_result(digest(path))
            except BaseException as exc:
                pending.set_exception(exc)
                raise
        return pending.result()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for offset in range(0, len(names), 1024):
            batch = names[offset:offset + 1024]
            signatures = pool.map(lambda name: signature(root / name, hash_file), batch)
            changed.extend(name for name, value in zip(batch, signatures) if previous.get(name) != value)
    metadata = json.dumps({'version': 1, 'deleted': deleted}).encode()
    with tarfile.open(destination, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo('manifest.json')
        info.size, info.mode = len(metadata), 0o600
        archive.addfile(info, io.BytesIO(metadata))
        for name in changed:
            path = root / name
            info = archive.gettarinfo(str(path), arcname='root/' + name)
            # GNU tar understands these PAX attributes, including POSIX ACLs
            # and executable file capabilities installed by package managers.
            for attribute in (os.listxattr(path, follow_symlinks=False) if hasattr(os, 'listxattr') else []):
                info.pax_headers['SCHILY.xattr.' + attribute] = os.getxattr(
                    path, attribute, follow_symlinks=False).decode('utf-8', 'surrogateescape')
            if info.isfile():
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    size = destination.stat().st_size
    if not 0 < size <= max_bytes:
        raise RuntimeError('Checkpoint exceeds the 4 GiB compressed archive limit')
    value = digest(destination)
    return {'size': size, 'sha256': value.hexdigest(), 'checksum': base64.b64encode(value.digest()).decode()}


def valid_name(name):
    return bool(name and not name.startswith('/') and '..' not in name.split('/') and not excluded(name))


def restore(archive, *, root=Path('/'), expected_sha256):
    if digest(archive).hexdigest() != expected_sha256:
        raise ValueError('Checkpoint checksum mismatch')
    with tarfile.open(archive, 'r:gz') as source:
        manifest = json.load(source.extractfile('manifest.json'))
        if manifest.get('version') != 1:
            raise ValueError('Unsupported checkpoint version')
        for name in manifest['deleted']:
            if not valid_name(name):
                raise ValueError('Invalid deletion in checkpoint')
        for member in source:
            if member.name == 'manifest.json':
                continue
            if not member.name.startswith('root/') or not valid_name(member.name[5:]) or member.isdev():
                raise ValueError('Invalid path in checkpoint')
            if member.islnk() and (not member.linkname.startswith('root/') or not valid_name(member.linkname[5:])):
                raise ValueError('Invalid hardlink in checkpoint')
    # Remove files deleted relative to the pinned base image, deepest first.
    # Never follow a symlink parent into VM-private paths or outside a test root.
    for name in sorted(manifest['deleted'], key=lambda n: n.count('/'), reverse=True):
        target = root / name
        parent = target.parent.resolve()
        if not parent.is_relative_to(root.resolve()):
            raise ValueError('Deletion escapes the filesystem')
        relative = parent.relative_to(root.resolve()).as_posix()
        if excluded(relative):
            raise ValueError('Deletion targets VM-private state')
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            target.rmdir()  # A nonempty, unexpected directory fails closed.
    result = subprocess.run(['tar', '--extract', '--gzip', '--file', str(archive), '--directory', str(root),
        '--xattrs', '--acls', '--numeric-owner', '--exclude=manifest.json', '--strip-components=1'], capture_output=True)
    if result.returncode:
        raise RuntimeError('Filesystem restoration failed')
    # No execution specs or credentials are imported and no saved process is
    # started. Durable harness journals retain their uncertain markers.
