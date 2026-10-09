"""Bounded recovery archives prioritize repository edits over installed packages."""
import json
import os
from pathlib import Path
import subprocess
import zipfile

EXCLUDED = {"moyai-captures", "node_modules", "__pycache__", "venv", "env", "site-packages", "dist", "build"}
FILE_LIMIT = 2 * 1024 * 1024
TOTAL_LIMIT = 15 * 1024 * 1024


def eligible(relative):
    return not any(part.startswith(".") or part in EXCLUDED for part in relative.parts)


def git(repo, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                          capture_output=True, check=True, timeout=20).stdout


def collect_archive(workspace, artifacts, token):
    workspace, artifacts = Path(workspace).resolve(), Path(artifacts)
    repositories, loose_files, omitted = [], [], []
    tracked_files = []
    directories = 0
    for current, dirs, files in os.walk(workspace, followlinks=False):
        directories += 1
        if directories > 10000:
            omitted.append("Directory scan limit reached")
            break
        root = Path(current)
        if (root / ".git").exists():
            repositories.append(root)
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if eligible(Path(d)) and not (root / d).is_symlink()
                         and not (root / d / "pyvenv.cfg").exists())
        loose_files.extend(root / name for name in sorted(files) if eligible(Path(name)))

    total = 0
    repo_details = []
    with zipfile.ZipFile(artifacts / "result.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        def add(name, data):
            nonlocal total
            if len(data) > FILE_LIMIT or total + len(data) > TOTAL_LIMIT:
                omitted.append(name + " (size limit)")
                return
            for secret in (token, os.environ.get('WORKSPACE_ACCESS_CLIENT_ID', '').encode(),
                           os.environ.get('WORKSPACE_ACCESS_CLIENT_SECRET', '').encode()):
                if secret:
                    data = data.replace(secret, b"[redacted]")
            archive.writestr(name, data)
            total += len(data)

        def add_file(path, name, root):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                return
            if path.stat().st_size > FILE_LIMIT:
                omitted.append(name + " (size limit)")
                return
            add(name, path.read_bytes())

        add_file(artifacts / "result.md", "result.md", artifacts.resolve())
        # Discover all nested checkouts before collecting loose files. A large
        # virtualenv cannot consume the archive budget before the actual edits.
        for repo in repositories[:50]:
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
        if len(repositories) > 50:
            omitted.append("Repository count limit reached")
        for path in loose_files[:1000]:
            add_file(path, "new-files/" + path.relative_to(workspace).as_posix(), workspace)
        if len(loose_files) > 1000:
            omitted.append("Loose file count limit reached")
        add_file(artifacts / "browser.png", "browser.png", artifacts.resolve())
        for path in dict.fromkeys(tracked_files[:1000]):
            add_file(path, "new-files/" + path.relative_to(workspace).as_posix(), workspace)
        if len(tracked_files) > 1000:
            omitted.append("Tracked file count limit reached")
        archive.writestr("recovery-manifest.json", json.dumps({
            "repositories": repo_details,
            "omitted": omitted,
            "note": "Bounded workspace files and uncommitted patches, not a complete workspace backup. Dependencies are excluded; see omitted for collection limits.",
        }, indent=2))
