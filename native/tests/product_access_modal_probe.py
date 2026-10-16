"""Run through MOYAI_E2E_RUNNER. Actual Modal component proof, not app/model E2E."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import modal

from moyai.product_credential_tools import COMMAND_GUEST, redact_output
from moyai.product_github_bootstrap import INSTALL, fetch_bundle


async def main():
    name = os.environ["MODAL_APP_NAME"]
    if not name.startswith("moyai-codex-e2e-"):
        raise ValueError("Use the scoped local E2E launcher.")
    report = {"kind": "actual_modal_component_checks", "actual_app_e2e": False}
    with tempfile.TemporaryDirectory(prefix="moyai-git-probe-") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=source, stderr=subprocess.DEVNULL,
                env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
            ).decode().strip()
        git("init", "-b", "main")
        git("config", "user.name", "Isolated compatibility probe")
        git("config", "user.email", "local@example.test")
        (source / "hello.txt").write_text("Fixture source\n")
        git("add", "hello.txt")
        git("commit", "-m", "Synthetic fixture")
        commit = git("rev-parse", "HEAD")
        bundle = await fetch_bundle(source.as_uri(), "main", commit, "synthetic-worker-only-token", root / "source.bundle")
        app = await modal.App.lookup.aio(name, create_if_missing=True)
        sandbox = await modal.Sandbox.create.aio(
            "sleep", "600", app=app, image=modal.Image.debian_slim(python_version="3.13").apt_install("git"),
            timeout=660, idle_timeout=300, workdir="/workspace",
        )
        async def command(*args, env=None):
            process = await sandbox.exec.aio(*args, env=env, timeout=240)
            await process.wait.aio()
            return process.returncode, await process.stdout.read.aio(), await process.stderr.read.aio()
        try:
            values = [
                {"request_id": "a" * 32, "revision": 1, "format": "env", "name": "probe", "env_var": "",
                 "value": json.dumps({"SERVICE_TOKEN": "synthetic-process-token"})},
                {"request_id": "b" * 32, "revision": 1, "format": "file", "name": "probe-file", "env_var": "SERVICE_CONFIG",
                 "value": "synthetic-file-token"},
            ]
            program = "import os,pathlib; print(os.environ['SERVICE_TOKEN']); print(pathlib.Path(os.environ['SERVICE_CONFIG']).read_text())"
            status, stdout, _ = await command("python3", "-I", "-c", COMMAND_GUEST, env={
                "MOYAI_NATIVE_CREDENTIAL_PAYLOAD": json.dumps({
                    "bindings": values, "command": "python3 -c " + shlex.quote(program), "timeout": 20, "names": [],
                }),
            })
            assert status == 0
            safe = redact_output(stdout, values)
            assert "synthetic-process-token" not in safe and "synthetic-file-token" not in safe
            assert safe.count("[credential redacted]") == 2
            status, stdout, _ = await command("python3", "-c", "import os;print('SERVICE_TOKEN' in os.environ or 'SERVICE_CONFIG' in os.environ)")
            assert status == 0 and stdout.strip() == "False"
            report["per_command_env_and_memfd_file"] = "passed"
            report["output_redaction_and_no_environment_persistence"] = "passed"

            receipt = {"repository_id": 123, "repository": "example/local-fixture", "branch": "main",
                       "commit_sha": commit, "working_dir": "/workspace/repo"}
            data = {"bundle_sha256": hashlib.sha256(bundle).hexdigest(), "marker": "synthetic-signed-receipt",
                    "branch": "main", "commit": commit, "url": "https://github.com/example/local-fixture.git",
                    "receipt": receipt}
            async def install():
                remote = "/tmp/moyai-git-" + uuid4().hex + ".bundle"
                await sandbox.filesystem.write_bytes.aio(bundle, remote)
                return await command("python3", "-I", "-c", INSTALL, json.dumps({**data, "bundle": remote}))
            status, stdout, _ = await install()
            assert status == 0 and json.loads(stdout) == receipt
            status, _, _ = await command("git", "-C", "/workspace/repo", "checkout", "-b", "human-work")
            assert status == 0
            await sandbox.filesystem.write_bytes.aio(b"Human edits remain\n", "/workspace/repo/hello.txt")
            status, stdout, _ = await install()
            assert status == 0 and json.loads(stdout) == receipt
            status, stdout, _ = await command("git", "-C", "/workspace/repo", "branch", "--show-current")
            assert status == 0 and stdout.strip() == "human-work"
            status, stdout, _ = await command("cat", "/workspace/repo/hello.txt")
            assert status == 0 and stdout == "Human edits remain\n"
            report["git_bundle_install_and_retry_preserve_branch_and_edits"] = "passed"
            status, _, _ = await command("mv", "/workspace/repo", "/workspace/retained-checkout")
            assert status == 0
            await sandbox.filesystem.write_bytes.aio(b"Unrelated user file\n", "/workspace/repo/human.txt")
            status, _, _ = await install()
            assert status != 0
            status, stdout, _ = await command("cat", "/workspace/repo/human.txt")
            assert status == 0 and stdout == "Unrelated user file\n"
            report["unrelated_existing_checkout_refused"] = "passed"
        finally:
            await sandbox.terminate.aio()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
