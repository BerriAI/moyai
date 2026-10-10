"""Versioned public procedures shipped with the agent; no private library copies."""
import hashlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).parent


def content(name):
    if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in name):
        raise ValueError('Use an exact bundled skill name from the catalog.')
    root = ROOT / name
    if not (root / 'SKILL.md').is_file():
        raise ValueError('Bundled skill not found.')
    return {path.relative_to(root).as_posix(): path.read_text(encoding='utf-8')
            for path in sorted(root.rglob('*')) if path.is_file() and not path.is_symlink()}


def revision(files):
    digest = hashlib.sha256()
    for path, text in sorted(files.items()):
        digest.update(path.encode() + b'\0' + text.encode() + b'\0')
    return digest.hexdigest()


def catalog():
    result = []
    for path in sorted(ROOT.glob('*/SKILL.md')):
        files = content(path.parent.name)
        lines = files['SKILL.md'].splitlines()
        description = next(line.partition(':')[2].strip() for line in lines if line.startswith('description:'))
        result.append({'name': path.parent.name, 'description': description, 'revision': revision(files)})
    return result


def read(name, expected_revision, path='SKILL.md', offset=0, characters=None):
    files = content(name)
    version = revision(files)
    if expected_revision != version:
        raise ValueError('This bundled skill version changed. Use the current catalog revision before loading it.')
    if path not in files or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
        raise ValueError('File not found in the selected skill. Use its listed supporting paths.')
    value = files[path]
    if offset > len(value):
        raise ValueError('The offset is past the end of this file.')
    end = len(value) if characters is None else min(len(value), offset + characters)
    return {'name': name, 'revision': version, 'path': path, 'content': value[offset:end],
            'offset': offset, 'characters': len(value), 'next_offset': end if end < len(value) else None,
            'files': [{'path': p, 'characters': len(t), 'sha256': hashlib.sha256(t.encode()).hexdigest()} for p, t in files.items()]}
