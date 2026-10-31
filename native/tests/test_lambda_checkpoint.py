"""Filesystem invariants, supplementary to actual-app AWS validation."""
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(os.environ.get(
    "MOYAI_LAMBDA_GUEST_SOURCE", Path(__file__).parents[1] / "lambda_guest/checkpoint.py",
))
spec = importlib.util.spec_from_file_location("lambda_checkpoint", SOURCE)
assert spec and spec.loader
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


@pytest.fixture
def image(tmp_path):
    root = tmp_path / "image"
    (root / "lib").mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "lib/dependency").write_bytes(b"unchanged dependency" * 4096)
    (root / "lib/edit").write_bytes(b"abc")
    (root / "lib/deleted").write_text("delete me")
    # Give real inode change times enough distance from the conservative guard.
    time.sleep(1.05)
    baseline = tmp_path / "baseline.json"
    checkpoint.baseline(root, baseline)
    return root, baseline


def pack(tmp_path, root, baseline, cache, name="checkpoint"):
    path = tmp_path / f"{name}.tar.gz"
    meta = checkpoint.pack(path, root=root, baseline_path=baseline, max_bytes=10**7, content_cache=cache)
    with tarfile.open(path) as archive:
        names = archive.getnames()
    return path, meta, names


def test_first_and_repeated_tiny_saves_reuse_image_hashes(image, tmp_path):
    root, baseline = image
    (root / "workspace/probe").write_bytes(b"42\n")
    cache = checkpoint.ContentCache()
    for name in ("first", "second"):
        _, meta, names = pack(tmp_path, root, baseline, cache, name)
        assert "root/workspace/probe" in names
        assert "root/lib/dependency" not in names
        assert meta["diagnostics"]["bytes_hashed"] <= 3
        assert meta["diagnostics"]["cache_hits"] >= 3


def test_same_size_same_mtime_and_inode_replacement_cannot_hide_content(image, tmp_path):
    root, baseline = image
    cache = checkpoint.ContentCache()
    target = root / "lib/edit"
    original = target.stat()
    for index, data in enumerate((b"def", b"ghi")):
        if index:
            target.unlink()
        target.write_bytes(data)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        _, meta, names = pack(tmp_path, root, baseline, cache, str(index))
        assert "root/lib/edit" in names
        assert meta["diagnostics"]["bytes_hashed"] == 3


@pytest.mark.parametrize("seed", ["missing", "invalid_json", "mismatch", "malformed_stamp"])
def test_untrusted_or_missing_hash_seed_falls_back_to_content(image, tmp_path, seed):
    root, baseline = image
    hints = baseline.with_suffix(".stats.json")
    value = json.loads(hints.read_text())
    if seed == "missing":
        hints.unlink()
    elif seed == "invalid_json":
        hints.write_text("{")
    else:
        if seed == "mismatch":
            value["baseline_sha256"] = "wrong"
        else:
            value["files"] = {"lib/dependency": [0]}
        hints.write_text(json.dumps(value))
    _, meta, names = pack(tmp_path, root, baseline, checkpoint.ContentCache())
    assert meta["diagnostics"]["bytes_hashed"] >= (root / "lib/dependency").stat().st_size
    assert names == ["manifest.json"]


def test_each_archive_retains_installed_dependencies(image, tmp_path):
    root, baseline = image
    (root / "workspace/site-packages").mkdir()
    (root / "workspace/site-packages/installed.py").write_text("VALUE = 42\n")
    cache = checkpoint.ContentCache()
    for name in ("first", "second"):
        _, _, names = pack(tmp_path, root, baseline, cache, name)
        assert "root/workspace/site-packages/installed.py" in names
        assert "root/lib/dependency" not in names


def test_recent_ctime_and_backwards_clock_invalidate_cache(image, tmp_path, monkeypatch):
    root, baseline = image
    stamp = checkpoint.content_stamp((root / "lib/edit").stat())
    assert not checkpoint.stable_stamp(stamp, stamp[-1])
    cache = checkpoint.ContentCache()
    pack(tmp_path, root, baseline, cache)
    monkeypatch.setattr(checkpoint.time, "time_ns", lambda: cache.observed_ns - 1)
    _, meta, _ = pack(tmp_path, root, baseline, cache, "clock")
    assert meta["diagnostics"]["cache_hits"] == 0


def test_changed_hardlink_topology_even_with_matching_link_counts(tmp_path):
    root = tmp_path / "image"
    root.mkdir()
    for name in ("a", "c"):
        (root / name).write_bytes(b"same")
    os.link(root / "a", root / "b")
    os.link(root / "c", root / "d")
    baseline = tmp_path / "base.json"
    checkpoint.baseline(root, baseline)
    (root / "b").unlink()
    (root / "d").unlink()
    os.link(root / "a", root / "d")
    os.link(root / "c", root / "b")
    _, meta, names = pack(tmp_path, root, baseline, checkpoint.ContentCache())
    assert all("root/" + name in names for name in "abcd")
    assert meta["diagnostics"]["signature_changes"]["hardlinks"] == 4


def test_restore_keeps_matching_mandatory_security_label(tmp_path, monkeypatch):
    """Lambda refuses to remove security.selinux, including for guest root."""
    root = tmp_path / "guest"
    (root / "workspace").mkdir(parents=True)
    archive = tmp_path / "checkpoint.tar.gz"
    label = b"system_u:object_r:container_file_t:s0"
    with tarfile.open(archive, "w:gz") as target:
        manifest = json.dumps({"version": 1, "deleted": []}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        target.addfile(info, io.BytesIO(manifest))
        directory = tarfile.TarInfo("root/workspace")
        directory.type = tarfile.DIRTYPE
        directory.pax_headers["SCHILY.xattr.security.selinux"] = label.decode()
        target.addfile(directory)
    monkeypatch.setattr(checkpoint.os, "listxattr", lambda *a, **k: ["security.selinux"], raising=False)
    monkeypatch.setattr(checkpoint.os, "getxattr", lambda *a, **k: label, raising=False)

    def denied(*args, **kwargs):
        raise PermissionError("Mandatory security label cannot be removed or rewritten")

    monkeypatch.setattr(checkpoint.os, "removexattr", denied, raising=False)
    monkeypatch.setattr(checkpoint.os, "setxattr", denied, raising=False)
    # No portable way to create a real SELinux label on macOS; actual guest
    # restoration is additionally exercised by the AWS deployment acceptance run.
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    checkpoint.restore(archive, root=root, expected_sha256=checkpoint.digest(archive).hexdigest())


@pytest.mark.skipif(sys.platform != "linux", reason="GNU tar / Linux xattr restore is verified on ARM64")
def test_independent_archives_round_trip_metadata_deletions_and_restored_cache(image, tmp_path):
    root, baseline = image
    os.setxattr(root / "workspace", "user.removed", b"baseline")
    checkpoint.baseline(root, baseline)
    pristine = tmp_path / "pristine"
    shutil.copytree(root, pristine)
    cache = checkpoint.ContentCache()
    os.setxattr(root / "workspace", "user.marker", b"first")
    os.link(root / "lib/dependency", root / "workspace/image-link")
    (root / "workspace/target").write_bytes(b"42\n")
    os.link(root / "workspace/target", root / "workspace/hard")
    (root / "workspace/symbolic").symlink_to("hard")
    os.chmod(root / "workspace/target", 0o640)
    (root / "lib/deleted").unlink()
    mtime = (root / "lib/edit").stat().st_mtime_ns
    for index in (1, 2):
        (root / "lib/edit").write_bytes(b"def" if index == 1 else b"ghi")
        os.utime(root / "lib/edit", ns=(mtime, mtime))
        if index == 2:
            os.removexattr(root / "workspace", "user.marker")
            os.removexattr(root / "workspace", "user.removed")
        archive, meta, _ = pack(tmp_path, root, baseline, cache, f"save{index}")
        fresh = tmp_path / f"fresh{index}"
        shutil.copytree(pristine, fresh)
        checkpoint.restore(archive, root=fresh, expected_sha256=meta["sha256"])
        assert (fresh / "lib/edit").read_bytes() == (b"def" if index == 1 else b"ghi")
        assert not (fresh / "lib/deleted").exists()
        assert (fresh / "workspace/symbolic").read_bytes() == b"42\n"
        assert (fresh / "workspace/hard").stat().st_ino == (fresh / "workspace/target").stat().st_ino
        assert (fresh / "workspace/image-link").stat().st_ino == (fresh / "lib/dependency").stat().st_ino
        assert (fresh / "workspace/target").stat().st_mode & 0o777 == 0o640
        assert ("user.marker" in os.listxattr(fresh / "workspace")) == (index == 1)
        assert ("user.removed" in os.listxattr(fresh / "workspace")) == (index == 1)
        _, _, names = pack(tmp_path, fresh, baseline, checkpoint.ContentCache(), f"restored{index}")
        assert "root/lib/edit" in names and "root/workspace/target" in names


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="Linux file capabilities require the root ARM64 guest",
)
def test_linux_security_capability_and_posix_acl_round_trip(image, tmp_path):
    root, baseline = image
    pristine = tmp_path / "pristine"
    shutil.copytree(root, pristine)
    target = root / "lib/edit"
    # Linux vfs_cap_data revision 2, effective CAP_NET_BIND_SERVICE. This only
    # changes an inert fixture's metadata; the file is never executed.
    capability = struct.pack("<IIIII", 0x02000001, 1 << 10, 0, 0, 0)
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in (
        (1, 6, 0xffffffff), (2, 4, 1234), (4, 4, 0xffffffff),
        (16, 4, 0xffffffff), (32, 0, 0xffffffff),
    ))
    os.setxattr(target, "system.posix_acl_access", acl)
    os.setxattr(target, "security.capability", capability)
    expected = checkpoint.attributes(target)
    cache = checkpoint.ContentCache()
    for index in (1, 2):
        archive, meta, _ = pack(tmp_path, root, baseline, cache, f"security-{index}")
        fresh = tmp_path / f"security-restore-{index}"
        shutil.copytree(pristine, fresh)
        checkpoint.restore(archive, root=fresh, expected_sha256=meta["sha256"])
        assert checkpoint.attributes(fresh / "lib/edit") == expected
        assert (fresh / "lib/edit").stat().st_mode == target.stat().st_mode
        again, saved, _ = pack(tmp_path, fresh, baseline, checkpoint.ContentCache(), f"security-again-{index}")
        another = tmp_path / f"security-again-restore-{index}"
        shutil.copytree(pristine, another)
        checkpoint.restore(again, root=another, expected_sha256=saved["sha256"])
        assert checkpoint.attributes(another / "lib/edit") == expected
