"""Credential-free guest program: freeze and export explicitly staged changes."""

from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 1000


class MissingBaseCommit(ValueError):
    """The selected remote base has not been imported into this checkout."""


def export(directory: str, base_sha: str) -> list[dict]:
    root = pathlib.Path(directory)
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("Use the checkout's absolute directory, without symlinks.")

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=root, check=True, capture_output=True,
        ).stdout

    if pathlib.Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("Use the repository root.")
    try:
        git("cat-file", "-e", base_sha + "^{commit}")
    except subprocess.CalledProcessError:
        raise MissingBaseCommit("The publication base commit is missing from the local checkout.") from None
    # An immutable tree makes the export coherent even if the index later changes.
    tree = git("write-tree").decode().strip()
    diff = git("diff-tree", "-r", "--no-ext-diff", "--no-renames", "--no-abbrev", "-z", base_sha, tree)
    entries = diff.split(b"\0")
    if entries[-1] != b"" or len(entries[:-1]) % 2:
        raise ValueError("Invalid staged diff.")
    files, total = [], 0
    for header, raw_path in zip(entries[0:-1:2], entries[1:-1:2], strict=True):
        old_mode, new_mode, _old, oid, _status = header.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if new_mode not in {"000000", "100644", "100755"} or old_mode not in {":000000", ":100644", ":100755"}:
            raise ValueError("Symlinks and submodules cannot be published as ordinary files.")
        if len(files) >= MAX_FILES:
            raise ValueError("Publish at most 1000 changed files at a time.")
        if new_mode == "000000":
            files.append({"path": path, "content": None})
            continue
        size = int(git("cat-file", "-s", oid))
        total += size
        if total > MAX_BYTES:
            raise ValueError("Staged publication exceeds 20 MiB.")
        content = git("cat-file", "blob", oid)
        if len(content) != size:
            raise ValueError("Git object size changed.")
        files.append({"path": path, "content": base64.b64encode(content).decode("ascii"),
                      "encoding": "base64", "executable": new_mode == "100755"})
    if not files:
        raise ValueError("No staged changes relative to base_sha. Stage the intended files first.")
    return files


if __name__ == "__main__":
    try:
        print(json.dumps({"files": export(sys.argv[1], sys.argv[2])}))
    except MissingBaseCommit as error:
        print(json.dumps({"code": "missing_base", "error": str(error)}))
        sys.exit(1)
    except (ValueError, subprocess.CalledProcessError, UnicodeError) as error:
        # Do not echo subprocess output, which can contain repository secrets.
        print(json.dumps({"error": str(error) if isinstance(error, ValueError) else "The staged Git tree could not be read."}))
        sys.exit(1)
