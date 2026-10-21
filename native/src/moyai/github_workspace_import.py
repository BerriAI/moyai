"""Import an authenticated base bundle without changing checkout files or refs."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys


def import_base(directory: str, bundle_path: str, digest: str, commit: str) -> None:
    root, bundle = pathlib.Path(directory), pathlib.Path(bundle_path)
    if (
        root.is_symlink() or root.resolve() != root or not root.is_dir()
        or (root / ".git").is_symlink()
    ):
        raise ValueError("Use the checkout's absolute directory, without symlinks.")
    if not re.fullmatch(r"/tmp/moyai-git-[0-9a-f]{32}\.bundle", bundle_path):
        raise ValueError("Invalid base bundle path.")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid base commit.")

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=root, check=True, capture_output=True, timeout=120,
        ).stdout

    try:
        if bundle.is_symlink() or hashlib.sha256(bundle.read_bytes()).hexdigest() != digest:
            raise ValueError("Base bundle integrity check failed.")
        if pathlib.Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
            raise ValueError("Use the repository root.")
        git("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            "--no-auto-maintenance", str(bundle), commit)
        git("cat-file", "-e", commit + "^{commit}")
    finally:
        bundle.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        import_base(*sys.argv[1:])
        print(json.dumps({"imported": True}))
    except (ValueError, OSError, subprocess.SubprocessError):
        # Git diagnostics may contain private paths or configured transport data.
        print(json.dumps({"error": "Could not import the exact publication base into the checkout."}))
        sys.exit(1)
