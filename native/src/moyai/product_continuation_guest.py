"""Trusted, stdlib-only recovery code copied into isolated Modal computers.

It never starts an agent or reads a home directory/checkpoint/credential store.
Legacy recovery packages remain intact; extraction refuses escapes and links.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_ENTRIES = 20_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_CATALOG_BYTES = 16 * 1024 * 1024
CHUNK = 1024 * 1024


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value or value != path.as_posix() or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Unsafe recovery path.")
    return path


def private_path(path: PurePosixPath) -> bool:
    # Match the existing recovery collector's explicit contract. Do not try to
    # infer that an arbitrary dotfile (.npmrc, .netrc, .git-credentials, etc.) is safe.
    return any(part.startswith(".") for part in path.parts)


def bounded_zip(path: Path) -> zipfile.ZipFile:
    """Bound the central directory BEFORE ZipFile materializes its entry list."""
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError("Recovery package exceeds the import byte budget.")
    with path.open("rb") as stream:
        stream.seek(max(0, size - 65_557))
        tail = stream.read(65_557)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise ValueError("Recovery package has no complete directory.")
    _, disk, directory_disk, disk_count, count, directory_size, offset, comment = struct.unpack_from(
        "<4s4H2LH", tail, position,
    )
    if (
        disk or directory_disk or disk_count != count or count > MAX_ENTRIES
        or directory_size > MAX_CATALOG_BYTES or offset + directory_size > size
        or position + 22 + comment != len(tail) or count == 65535
    ):
        raise ValueError("Recovery directory is unsupported or exceeds its bounds.")
    # ZipFile parses the entire central directory rather than trusting EOCD's
    # entry count. Validate its actual record count before allocating ZipInfo objects.
    seen, consumed = 0, 0
    with path.open("rb") as stream:
        stream.seek(offset)
        while consumed < directory_size:
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ValueError("Recovery package has an invalid directory.")
            name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
            length = name_size + extra_size + comment_size
            consumed += 46 + length
            seen += 1
            if consumed > directory_size or seen > MAX_ENTRIES or seen > count:
                raise ValueError("Recovery directory exceeds its recorded bounds.")
            stream.seek(length, 1)
    if seen != count:
        raise ValueError("Recovery directory entry count is inconsistent.")
    archive = zipfile.ZipFile(path)
    if len(archive.infolist()) != count or len({entry.filename for entry in archive.infolist()}) != count:
        archive.close()
        raise ValueError("Recovery package contains duplicate or inconsistent entries.")
    return archive


def archive_manifest(path: Path) -> dict:
    """Verify every imported byte and return references, never extracted paths."""
    files, total, excluded = [], 0, []
    with bounded_zip(path) as archive:
        for entry in archive.infolist():
            relative_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir():
                continue
            if mode not in {0, stat.S_IFREG} or entry.flag_bits & 1 or entry.compress_type not in {
                zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
            }:
                raise ValueError("Recovery packages must contain ordinary, unencrypted files.")
            if entry.file_size < 0 or total + entry.file_size > MAX_BYTES:
                raise ValueError("Restored files exceed the import byte budget.")
            total += entry.file_size
            if not entry.filename.startswith("new-files/"):
                continue
            target = relative_path(entry.filename.removeprefix("new-files/"))
            if private_path(target):
                excluded.append(target.as_posix())
                continue
            digest, count = hashlib.sha256(), 0
            with archive.open(entry) as source:
                while chunk := source.read(CHUNK):
                    count += len(chunk)
                    if count > entry.file_size:
                        raise ValueError("Recovery bytes exceeded their declared size.")
                    digest.update(chunk)
            if count != entry.file_size:
                raise ValueError("Recovery file is incomplete.")
            files.append({"member": entry.filename, "path": target.as_posix(),
                          "size": count, "sha256": digest.hexdigest()})
        recovery = {}
        if "recovery-manifest.json" in archive.namelist():
            entry = archive.getinfo("recovery-manifest.json")
            if entry.file_size > CHUNK:
                raise ValueError("Recovery metadata exceeds its bound.")
            recovery = json.loads(archive.read(entry))
            if not isinstance(recovery, dict):
                raise ValueError("Recovery metadata must be an object.")
    return {
        "files": files, "bytes": sum(item["size"] for item in files),
        "repositories": recovery.get("repositories", []),
        "omitted": recovery.get("omitted", []), "excluded_private_files": len(excluded),
        "note": recovery.get("note", ""),
    }


def checked_root(value: str) -> Path:
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError("Use a real, absolute workspace directory.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def parent_directory(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise ValueError("Recovery would cross a symlink or non-directory.")
        else:
            current.mkdir()
    return current


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("The restore target is not an ordinary file.")
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def restore_archive(package: str, directory: str, expected_manifest: str) -> dict:
    root, source_path = checked_root(directory), Path(package)
    manifest = archive_manifest(source_path)
    manifest_digest = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    if manifest_digest != expected_manifest:
        raise ValueError("Recovery manifest changed before restoration.")
    restored = 0
    with bounded_zip(source_path) as archive:
        for item in manifest["files"]:
            relative = relative_path(item["path"])
            parent = parent_directory(root, relative)
            target = parent / relative.name
            if target.exists() or target.is_symlink():
                if digest_file(target) == item["sha256"]:
                    restored += 1
                    continue
                # Bootstrap restores into a fresh computer. Never overwrite
                # differing data left by a user or an ambiguous prior operation.
                raise ValueError("A different file already occupies a recovery target.")
            descriptor, name = tempfile.mkstemp(prefix=".moyai-restore-", dir=parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as out:
                    digest, size = hashlib.sha256(), 0
                    with archive.open(item["member"]) as source:
                        while chunk := source.read(CHUNK):
                            size += len(chunk)
                            if size > item["size"]:
                                raise ValueError("Recovery bytes exceeded their bound.")
                            digest.update(chunk)
                            out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError("Recovered file failed integrity verification.")
                os.link(temporary, target, follow_symlinks=False)
                restored += 1
            finally:
                temporary.unlink(missing_ok=True)
    return {"files": restored, "bytes": manifest["bytes"], "manifest_fingerprint": manifest_digest}


def export_workspace(collector: str, directory: str, output: str) -> dict:
    """Reuse the existing reviewed recovery collector inside a network-blocked clone."""
    import resource
    import runpy

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    root = checked_root(directory)
    if root != Path("/workspace") and Path("/workspace") not in root.parents:
        raise ValueError("Only application workspace files can be exported.")
    destination = checked_root(output)
    module = runpy.run_path(collector)
    module["collect_archive"](root, destination, b"")
    package = destination / "result.zip"
    return {"package": str(package), "sha256": digest_file(package), "size": package.stat().st_size}


if __name__ == "__main__":
    try:
        if sys.argv[1] == "inspect":
            result = archive_manifest(Path(sys.argv[2]))
        elif sys.argv[1] == "restore":
            result = restore_archive(*sys.argv[2:5])
        elif sys.argv[1] == "export":
            result = export_workspace(*sys.argv[2:5])
        else:
            raise ValueError("Unknown recovery operation.")
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        # Never print source file bytes, commands, credentials or provider errors.
        print(json.dumps({"error": type(error).__name__}))
        raise SystemExit(1) from None
