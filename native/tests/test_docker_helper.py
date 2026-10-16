from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE_DOCKER = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
counter = Path(os.environ["FAKE_COUNTER"])
ticks = int(counter.read_text()) if counter.exists() else 0
with Path(os.environ["FAKE_LOG"]).open("a") as f:
    f.write(json.dumps({"args": args, "ticks": ticks,
        "project": os.getenv("COMPOSE_PROJECT_NAME"), "files": os.getenv("COMPOSE_FILE"),
        "image": os.getenv("MOYAI_IMAGE"), "envfiles": os.getenv("COMPOSE_ENV_FILES")}) + "\n")
if args[0] == "compose":
    rest = args[1:]
    while rest[0].startswith("--"):
        rest = rest[2:]
    if rest[0] == "version":
        print(os.getenv("FAKE_VERSION", "2.30.0"))
    elif rest[0] == "config":
        print("setup\napi\nbroker\nworker\ncoordinator\nproxy\nactivate")
    elif rest[0] == "ps":
        print("activation-container")
    elif rest[0] == "run":
        if rest[-1] == "validate":
            sys.exit(int(os.getenv("FAKE_VALIDATE_EXIT", "0")))
        if rest[-1] == "ready":
            print('{"installation":"serving","model_execution":"not_checked"}')
elif args[0] == "inspect":
    if "Running" in args[2]:
        counter.write_text(str(ticks + 1))
        print("true" if ticks < int(os.getenv("FAKE_PENDING_POLLS", "1")) else "false")
    else:
        print(os.getenv("FAKE_ACTIVATION_EXIT", "0"))
'''


@pytest.fixture
def helper(tmp_path):
    directory = tmp_path / "bundle with spaces"
    directory.mkdir()
    shutil.copy2(ROOT / "deploy/docker/moyai.sh", directory / "moyai.sh")
    marker = tmp_path / "must-not-be-created"
    (directory / ".env").write_text(
        "MOYAI_IMAGE=registry.example/moyai@sha256:" + "a" * 64 + "\n"
        "COMPOSE_PROJECT_NAME=owned-installation\n"
        "COMPOSE_FILE=compose.split.yaml:compose.temporal.yaml\n"
        "MOYAI_PORT=8787\nMOYAI_BIND_ADDRESS=127.0.0.1\n"
        "MOYAI_TEMPORAL_DATABASE_PASSWORD=synthetic-temporal\n"
        f'LITELLM_API_KEY="$(touch {marker})#literal"\n'
    )
    executable = tmp_path / "docker"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o755)
    environment = {
        **os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "FAKE_LOG": str(tmp_path / "calls.jsonl"), "FAKE_COUNTER": str(tmp_path / "polls"),
        "COMPOSE_PROJECT_NAME": "unrelated", "COMPOSE_FILE": "/unrelated.yaml", "MOYAI_IMAGE": "unrelated",
    }
    return directory, environment, marker


def run(helper, **changes):
    directory, environment, marker = helper
    result = subprocess.run(
        ["sh", str(directory / "moyai.sh"), "up", "--timeout", "0002"],
        cwd=directory.parent, env={**environment, **changes}, capture_output=True, text=True, timeout=15, check=False,
    )
    log = Path(environment["FAKE_LOG"])
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert not marker.exists()
    return result, calls


def test_up_waits_for_activation_and_uses_only_this_bundles_project(helper):
    result, calls = run(helper)
    assert result.returncode == 0, result.stderr
    assert '"model_execution":"not_checked"' in result.stdout
    validate = next(i for i, call in enumerate(calls) if call["args"][-1] == "validate")
    up = next(i for i, call in enumerate(calls) if "up" in call["args"])
    ready = next(i for i, call in enumerate(calls) if call["args"][-1] == "ready")
    assert validate < up < ready
    assert calls[ready]["ticks"] == 2
    for call in calls[validate:]:
        assert call["project"] == "owned-installation"
        assert call["image"].startswith("registry.example/moyai@sha256:")
        assert call["files"] == "compose.split.yaml:compose.temporal.yaml"
        assert call["envfiles"] == "/dev/null"


def test_preflight_failure_does_not_start_or_replace_services(helper):
    result, calls = run(helper, FAKE_VALIDATE_EXIT="9")
    assert result.returncode != 0
    assert not any("up" in call["args"] for call in calls)


def test_failed_activation_never_reports_readiness(helper):
    result, calls = run(helper, FAKE_PENDING_POLLS="0", FAKE_ACTIVATION_EXIT="7")
    assert result.returncode != 0
    assert "Activation failed" in result.stderr
    assert not any(call["args"][-1] == "ready" for call in calls)


def test_activation_timeout_never_reports_readiness(helper):
    result, calls = run(helper, FAKE_PENDING_POLLS="100")
    assert result.returncode != 0
    assert "Activation is still pending" in result.stderr
    assert not any(call["args"][-1] == "ready" for call in calls)


@pytest.mark.parametrize("version", ["2.29.9", ".", "unavailable"])
def test_unsupported_compose_cannot_start_anything(helper, version):
    result, calls = run(helper, FAKE_VERSION=version)
    assert result.returncode != 0
    assert len(calls) == 1
