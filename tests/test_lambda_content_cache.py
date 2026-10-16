"""Content reuse must preserve independent deltas and same-size/mtime edits."""
import json
import os
from pathlib import Path
import tarfile

import pytest

from sandbox import lambda_checkpoint as checkpoint


@pytest.fixture
def image(tmp_path):
    root, work = tmp_path / 'image', tmp_path / 'work'
    root.mkdir()
    work.mkdir()
    library = root / 'opt/env/lib'
    library.mkdir(parents=True)
    (library / 'installed.py').write_text('original')
    base = work / 'base.json'
    checkpoint.baseline(root, base)
    return root, work, base


def pack(image, cache=None, progress=None):
    root, work, base = image
    destination = work / 'checkpoint.tar.gz'
    result = checkpoint.pack(destination, root=root, baseline_path=base,
                             max_bytes=1024 * 1024, content_cache=cache, progress=progress)
    with tarfile.open(destination) as archive:
        names = archive.getnames()
    return result, names


def test_seed_skips_unchanged_image_content_without_omitting_new_dependencies(image, monkeypatch):
    root, _, _ = image
    original = checkpoint.digest
    reads = []
    def digest(path):
        if path.is_relative_to(root):
            reads.append(path.relative_to(root).as_posix())
        return original(path)
    monkeypatch.setattr(checkpoint, 'digest', digest)
    cache = checkpoint.ContentCache()
    result, names = pack(image, cache)
    assert reads == []
    assert names == ['manifest.json']
    assert result['diagnostics']['cache_hits'] == 1
    installed = root / 'opt/env/lib/new.py'
    installed.write_text('new dependency')
    first, names = pack(image, cache)
    assert reads == ['opt/env/lib/new.py']
    assert 'root/opt/env/lib/new.py' in names
    # A later delta is still against the immutable image: it must contain the
    # installed dependency even though its content hash can now be reused.
    second, names = pack(image, cache)
    assert reads == ['opt/env/lib/new.py']
    assert 'root/opt/env/lib/new.py' in names
    assert second['diagnostics']['files_hashed'] == 0
    assert first['diagnostics']['bytes_hashed'] == len('new dependency')


def test_cached_content_detects_same_size_and_mtime_edits_and_restores(image, tmp_path):
    root, work, _ = image
    path = root / 'opt/env/lib/installed.py'
    cache = checkpoint.ContentCache()
    pack(image, cache)
    stamp = path.stat()
    for content in ('modified', 'new-data'):
        path.write_text(content)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert path.stat().st_size == stamp.st_size
        assert path.stat().st_mtime_ns == stamp.st_mtime_ns
        result, names = pack(image, cache)
        assert result['diagnostics']['files_hashed'] == 1
        assert 'root/opt/env/lib/installed.py' in names
        target = tmp_path / content
        (target / 'opt/env/lib').mkdir(parents=True)
        (target / 'opt/env/lib/installed.py').write_text('original')
        checkpoint.restore(work / 'checkpoint.tar.gz', root=target, expected_sha256=result['sha256'])
        assert (target / 'opt/env/lib/installed.py').read_text() == content


def test_metadata_change_still_enters_delta_when_content_is_unchanged(image):
    root, _, _ = image
    cache = checkpoint.ContentCache()
    pack(image, cache)
    path = root / 'opt/env/lib/installed.py'
    path.chmod(0o755)
    _, names = pack(image, cache)
    assert 'root/opt/env/lib/installed.py' in names


def test_replacement_and_deletion_do_not_reuse_old_content(image):
    root, _, _ = image
    cache = checkpoint.ContentCache()
    pack(image, cache)
    path = root / 'opt/env/lib/installed.py'
    stamp = path.stat()
    replacement = root / 'replacement'
    replacement.write_text('replaced')
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(path)
    result, names = pack(image, cache)
    assert result['diagnostics']['files_hashed'] == 1
    assert 'root/opt/env/lib/installed.py' in names
    path.unlink()
    result, _ = pack(image, cache)
    assert result['diagnostics']['deleted_entries'] == 1
    assert 'opt/env/lib/installed.py' not in cache.entries


@pytest.mark.parametrize('seed', ['missing', 'different-baseline', 'different-filesystem'])
def test_unverified_seed_falls_back_to_full_content_reads(image, seed):
    _, _, base = image
    hints = base.with_suffix('.stats.json')
    if seed == 'missing':
        hints.unlink()
    else:
        value = json.loads(hints.read_text())
        if seed == 'different-baseline':
            value['baseline_sha256'] = 'wrong'
        else:
            for stamp in value['files'].values():
                stamp[0] += 1
        hints.write_text(json.dumps(value))
    result, names = pack(image)
    assert result['diagnostics']['files_hashed'] == 1
    assert names == ['manifest.json']


def test_progress_and_phase_timings_describe_actual_pack_work(image):
    events = []
    result, _ = pack(image, progress=events.append)
    assert [event['phase'] for event in events] == [
        'inventory', 'signatures', 'signatures', 'archive', 'checksum', 'packed',
    ]
    metrics = result['diagnostics']
    for phase in ('inventory', 'baseline_load', 'signature', 'archive', 'checksum'):
        assert 0 <= metrics[phase + '_seconds'] <= metrics['pack_seconds']
    assert events[-1]['archive_bytes'] == result['size']
    assert events[2]['processed'] == metrics['entries']


def test_image_baseline_is_recorded_once_before_ready_and_never_rebased(image, monkeypatch):
    from sandbox import lambda_guest as guest
    _, _, base = image
    monkeypatch.setattr(guest.checkpoint, 'BASELINE', base)
    monkeypatch.setattr(guest, 'BOOTSTRAPPED', False)
    calls = []
    def capture():
        calls.append(True)
        base.write_text('{}')
    monkeypatch.setattr(guest.checkpoint, 'baseline', capture)
    guest.prepare_image_baseline()
    assert not calls  # Service restarts retain the immutable baseline.
    base.unlink()
    guest.prepare_image_baseline()
    assert calls == [True]
    monkeypatch.setattr(guest, 'BOOTSTRAPPED', True)
    guest.prepare_image_baseline()
    assert calls == [True]
    base.unlink()
    with pytest.raises(RuntimeError, match='immutable image baseline'):
        guest.prepare_image_baseline()
    assert calls == [True]  # Missing data cannot authorize rebasing a live VM.
