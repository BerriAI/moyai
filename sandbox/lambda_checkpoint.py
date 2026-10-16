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
import time

# Runtime machinery, kernel mounts, host-injected networking, and per-exec
# credentials do not belong to another VM. Everything else is included, even
# ignored/untracked files, package databases, home directories, and /session.
EXCLUDED = ('proc', 'sys', 'dev', 'run', 'var/lib/moyai-runtime', 'opt/moyai-lambda',
            'etc/hostname', 'etc/hosts', 'etc/resolv.conf', '.dockerenv')
RUNTIME = Path('/var/lib/moyai-runtime')
BASELINE = Path('/opt/moyai-lambda/base.json')


def content_stamp(info):
    # Size/mtime alone miss edits followed by utime(). ctime cannot be set by
    # ordinary filesystem writes. Include inode identity to catch replacement,
    # and keep nanosecond precision; never carry a runtime cache across VMs.
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class ContentCache:
    """Content hashes bound to one filesystem and a verified image baseline."""

    def __init__(self):
        self.context = None
        self.entries = {}

    def prepare(self, root, baseline_path, raw, previous):
        context = (str(root.resolve()), str(baseline_path.resolve()), hashlib.sha256(raw).hexdigest())
        if context == self.context:
            return
        self.entries = {}
        self.context = context
        hints = baseline_path.with_suffix('.stats.json')
        if not hints.exists():
            return  # Older images remain correct, with a full first scan.
        seed = json.loads(hints.read_text())
        if seed.get('baseline_sha256') != context[2]:
            return
        self.entries = {
            name: (stamp, previous[name][4])
            for name, stamp in seed.get('files', {}).items()
            if name in previous and stat.S_ISREG(previous[name][0])
        }


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
        content = hash_file(path, info) if hash_file else digest(path).hexdigest()
    return [info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            # Container/image layers normalize file timestamps to whole seconds.
            content, attributes(path), int(info.st_mtime) if not stat.S_ISDIR(info.st_mode) else 0]


def baseline(root=Path('/'), target=BASELINE):
    target.parent.mkdir(parents=True, exist_ok=True)
    signatures, stamps = {}, {}
    for name in inventory(root):
        path = root / name
        before = path.lstat()
        signatures[name] = signature(path)
        after = path.lstat()
        if content_stamp(before) != content_stamp(after):
            raise RuntimeError('File changed while recording the image baseline')
        if stat.S_ISREG(after.st_mode):
            stamps[name] = content_stamp(after)
    raw = json.dumps(signatures).encode()
    target.write_bytes(raw)
    target.with_suffix('.stats.json').write_text(json.dumps({
        'baseline_sha256': hashlib.sha256(raw).hexdigest(), 'files': stamps,
    }))


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


def pack(destination, *, root=Path('/'), baseline_path=BASELINE, max_bytes,
         content_cache=None, progress=None):
    started = time.perf_counter()
    timings = {}
    counts = {'files_hashed': 0, 'bytes_hashed': 0, 'cache_hits': 0}
    def report(phase, **values):
        if progress:
            progress({'phase': phase, 'elapsed_seconds': time.perf_counter() - started,
                      **counts, **values})
    report('inventory')
    names = inventory(root)
    timings['inventory_seconds'] = time.perf_counter() - started
    phase_started = time.perf_counter()
    raw = baseline_path.read_bytes()
    previous = json.loads(raw)
    cache = content_cache if content_cache is not None else ContentCache()
    cache.prepare(root, baseline_path, raw, previous)
    timings['baseline_load_seconds'] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    report('signatures', processed=0, entries=len(names))
    replaced = {name for name in names if name in previous and
                stat.S_IFMT(previous[name][0]) != stat.S_IFMT((root / name).lstat().st_mode)}
    deleted = sorted((set(previous) - set(names)) | replaced, key=lambda n: n.count('/'), reverse=True)
    # An independent delta against the immutable image, never another runtime
    # checkpoint. Reuse a hash only while its full change stamp still matches.
    # Restores and same-size/mtime edits change ctime and therefore require reads.
    changed = []
    signature_changes = {}
    hashes, hashes_lock = {}, threading.Lock()
    next_entries = {}
    def hash_file(path, info):
        name = path.relative_to(root).as_posix()
        stamp = content_stamp(info)
        cached = cache.entries.get(name)
        with hashes_lock:
            if cached and cached[0] == stamp:
                counts['cache_hits'] += 1
                next_entries[name] = cached
                return cached[1]
            # Hardlinks still share one read when their cache entry is absent.
            key = tuple(stamp)
            pending = hashes.get(key)
            owner = pending is None
            if owner:
                pending = hashes[key] = Future()
        if owner:
            try:
                value = digest(path).hexdigest()
                if content_stamp(path.lstat()) != stamp:
                    raise RuntimeError('File changed while hashing checkpoint')
                with hashes_lock:
                    counts['files_hashed'] += 1
                    counts['bytes_hashed'] += info.st_size
                pending.set_result(value)
            except BaseException as exc:
                pending.set_exception(exc)
                raise
        value = pending.result()
        with hashes_lock:
            next_entries[name] = (stamp, value)
        return value
    with ThreadPoolExecutor(max_workers=8) as pool:
        for offset in range(0, len(names), 1024):
            batch = names[offset:offset + 1024]
            signatures = pool.map(lambda name: signature(root / name, hash_file), batch)
            for name, value in zip(batch, signatures):
                old = previous.get(name)
                if old == value:
                    continue
                changed.append(name)
                fields = (['added'] if old is None else [
                    field for field, before, after in zip(
                        ('mode', 'uid', 'gid', 'nlink', 'content', 'xattrs', 'mtime_seconds'), old, value
                    ) if before != after
                ])
                for field in fields:
                    signature_changes[field] = signature_changes.get(field, 0) + 1
            report('signatures', processed=min(offset + len(batch), len(names)),
                   entries=len(names), changed_entries=len(changed))
    timings['signature_seconds'] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    report('archive', changed_entries=len(changed), deleted_entries=len(deleted))
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
    timings['archive_seconds'] = time.perf_counter() - phase_started
    size = destination.stat().st_size
    if not 0 < size <= max_bytes:
        raise RuntimeError('Checkpoint exceeds the 4 GiB compressed archive limit')
    phase_started = time.perf_counter()
    report('checksum', archive_bytes=size)
    value = digest(destination)
    timings['checksum_seconds'] = time.perf_counter() - phase_started
    timings['pack_seconds'] = time.perf_counter() - started
    # Commit only a complete scan. Failed scans cannot introduce unverified
    # cache entries, and deleted paths cannot survive through inode reuse.
    cache.entries = next_entries
    report('packed', archive_bytes=size, changed_entries=len(changed), deleted_entries=len(deleted))
    return {'size': size, 'sha256': value.hexdigest(), 'checksum': base64.b64encode(value.digest()).decode(),
            'diagnostics': {**timings, **counts, 'entries': len(names),
                            'changed_entries': len(changed), 'deleted_entries': len(deleted),
                            'signature_changes': signature_changes}}


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
