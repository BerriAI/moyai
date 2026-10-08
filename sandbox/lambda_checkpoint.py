"""File-only MicroVM checkpoints. This module runs in the guest, never the web host."""
import base64
import hashlib
import json
import os
import io
from pathlib import Path
import stat
import subprocess
import tarfile

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


def signature(path):
    info = path.lstat()
    return [info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            # Container/image layers normalize file timestamps to whole seconds.
            os.readlink(path) if path.is_symlink() else digest(path).hexdigest() if path.is_file() else '',
            attributes(path), int(info.st_mtime) if not path.is_dir() else 0]


def baseline(root=Path('/'), target=BASELINE):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({name: signature(root / name) for name in inventory(root)}))


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
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
    changed = [name for name in names if previous.get(name) != signature(root / name)]
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
