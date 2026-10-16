"""Stream recovery files and patches; exclude dependencies and private dotfiles."""
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

EXCLUDED = {"moyai-captures", "node_modules", "__pycache__", "venv", "env", "site-packages", "dist", "build"}
CHUNK_BYTES = 1024 * 1024


def eligible(relative):
    return not any(part.startswith(".") or part in EXCLUDED for part in relative.parts)


def git(repo, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                          capture_output=True, check=True, timeout=20).stdout


def collect_archive(workspace, artifacts, token):
    workspace, artifacts = Path(workspace).resolve(), Path(artifacts)
    repositories, loose_files, omitted = [], [], []
    tracked_files = []
    for current, dirs, files in os.walk(workspace, followlinks=False):
        root = Path(current)
        if (root / ".git").exists():
            repositories.append(root)
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if eligible(Path(d)) and not (root / d).is_symlink()
                         and not (root / d / "pyvenv.cfg").exists())
        loose_files.extend(root / name for name in sorted(files) if eligible(Path(name)))

    repo_details = []
    with zipfile.ZipFile(artifacts / "result.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        secrets = [secret for secret in (token, os.environ.get('WORKSPACE_ACCESS_CLIENT_ID', '').encode(),
                                         os.environ.get('WORKSPACE_ACCESS_CLIENT_SECRET', '').encode()) if secret]
        span = max(map(len, secrets), default=1)-1

        def add_stream(name, source):
            # Carry original bytes across pages, including any secret crossing a
            # page edge. Do not cut or buffer a complete file to redact it.
            with archive.open(name, 'w', force_zip64=True) as target:
                carry = b''
                while chunk := source.read(CHUNK_BYTES):
                    data = carry + chunk
                    cut = max(0, len(data)-span)
                    for secret in secrets:
                        start = data.find(secret, max(0, cut-len(secret)+1))
                        if 0 <= start < cut < start+len(secret):
                            cut = start
                    output, carry = data[:cut], data[cut:]
                    for secret in secrets:
                        output = output.replace(secret, b'[redacted]')
                    target.write(output)
                for secret in secrets:
                    carry = carry.replace(secret, b'[redacted]')
                target.write(carry)

        def add(name, data):
            add_stream(name, io.BytesIO(data))

        def add_file(path, name, root):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                return
            try:
                with path.open('rb') as source:
                    add_stream(name, source)
            except OSError:
                omitted.append(name + ' (file collection failed)')

        add_file(artifacts / "result.md", "result.md", artifacts.resolve())
        # Discover all nested checkouts before collecting loose files. A large
        # virtualenv cannot consume the archive budget before the actual edits.
        for repo in repositories:
            relative = repo.relative_to(workspace)
            label = str(relative)
            try:
                head = git(repo, "rev-parse", "HEAD").decode().strip()
                patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--")
                patch_name = "changes.patch" if label == "." else f"repositories/{label}/changes.patch"
                add(patch_name, patch)
                repo_details.append({"path": label, "base_commit": head, "patch": patch_name})
                for raw in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
                    if not raw:
                        continue
                    name = Path(os.fsdecode(raw))
                    if name.is_absolute() or ".." in name.parts or not eligible(name):
                        continue
                    path = repo / name
                    add_file(path, "new-files/" + path.relative_to(workspace).as_posix(), workspace)
                # Committing a generated file must not make its download disappear
                # on the next conversational turn. Collect current tracked contents
                # after edits and loose files so repository source cannot crowd them out.
                for raw in git(repo, "ls-files", "--cached", "-z").split(b"\0"):
                    name = Path(os.fsdecode(raw))
                    if raw and not name.is_absolute() and ".." not in name.parts and eligible(name):
                        tracked_files.append(repo / name)
            except (OSError, subprocess.SubprocessError):
                omitted.append(label + " (repository collection failed)")
        for path in loose_files:
            add_file(path, "new-files/" + path.relative_to(workspace).as_posix(), workspace)
        add_file(artifacts / "browser.png", "browser.png", artifacts.resolve())
        for path in dict.fromkeys(tracked_files):
            add_file(path, "new-files/" + path.relative_to(workspace).as_posix(), workspace)
        archive.writestr("recovery-manifest.json", json.dumps({
            "repositories": repo_details,
            "omitted": omitted,
            "note": "Workspace files and uncommitted patches. Dependencies, hidden files and symlinks are excluded; see omitted for collection failures. Publish exact deliverables with artifact_publish.",
        }, indent=2))
