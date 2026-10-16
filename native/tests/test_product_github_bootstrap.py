"""Actual local Git objects; Modal/application qualification is separate."""

import asyncio
import os
import subprocess

from moyai.product_github_bootstrap import fetch_bundle


async def test_worker_bundle_contains_exact_commit_without_transport_credential(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=source, env=environment, stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-b", "main")
    git("config", "user.name", "Isolated protocol test")
    git("config", "user.email", "local@example.test")
    (source / "hello.txt").write_text("Verified source file\n")
    git("add", "hello.txt")
    git("commit", "-m", "Synthetic fixture")
    commit = git("rev-parse", "HEAD")
    token = "synthetic-token-must-not-enter-bundle"
    output = tmp_path / "source.bundle"
    raw = await asyncio.wait_for(fetch_bundle(source.as_uri(), "main", commit, token, output), 15)
    assert token.encode() not in raw
    copied = tmp_path / "copy"
    await asyncio.to_thread(subprocess.run, ["git", "clone", "--branch", "main", str(output), str(copied)], check=True, capture_output=True)
    assert (copied / "hello.txt").read_text() == "Verified source file\n"
    assert (await asyncio.to_thread(subprocess.check_output, ["git", "-C", str(copied), "rev-parse", "HEAD"])).decode().strip() == commit
    assert token not in (copied / ".git" / "config").read_text()
