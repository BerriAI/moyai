from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_deployment_package import install_environment as install_fixture
from test_deployment_package import prepared

from moyai import deployment
from moyai.deployment_config import InstallationError, read_state, write_state

install_environment = install_fixture


class NativeControl:
    """A transport that can lose a reply before or after its native receipt commits."""

    def __init__(self, values, *, paused, failure):
        self.failure = failure
        self.calls = []
        worker = {"pid": 42, "backend_start": "2026-10-16T00:00:00+00:00",
                  "source_policy": "a" * 64, "worker_policy": "b" * 64, "maintenance_drain": False}
        self.status = {
            "protocol": 1, "core": "pydantic-v1",
            "native_schema": values["MOYAI_NATIVE_SCHEMA"], "task_queue": values["MOYAI_NATIVE_TASK_QUEUE"],
            "generation": 4, "paused": paused, "request_key": "previous-operation",
            "requester": {"pid": 42, "backend_start": worker["backend_start"]},
            "source_policy": worker["source_policy"], "workers": [worker], "counts": {},
            "state": "drained" if paused else "running",
        }

    async def __call__(self, values, command, **kwargs):
        if command == "status":
            return deepcopy(self.status)
        intent = kwargs["intent"]
        self.calls.append((command, deepcopy(intent)))
        failure, self.failure = self.failure, None
        if failure == "before":
            raise OSError("Transport failed before commit.")
        if failure == "paused":
            return deepcopy(self.status)
        assert intent["expected_generation"] == self.status["generation"]
        self.status.update(
            generation=self.status["generation"] + 1, request_key=intent["request_key"],
            paused=command == "request", state="drained" if command == "request" else "running",
        )
        if failure == "after":
            raise OSError("Acknowledgement lost after commit.")
        if failure == "timeout":
            self.status["state"] = "draining"
        return deepcopy(self.status)


def setup(install_environment, monkeypatch, *, paused, failure):
    install_environment["MOYAI_INSTALL_TOPOLOGY"] = "split"
    state, values = prepared(install_environment)
    state.update(phase="initialized", activation_pending=False)
    if paused:
        state["upgrade"] = {"phase": "awaiting_resume", "generation": 4}
    write_state(install_environment, state)
    native = NativeControl(values, paused=paused, failure=failure)
    monkeypatch.setattr(deployment, "control", native)
    monkeypatch.setattr(deployment, "health", AsyncMock())
    return state, native


@pytest.mark.parametrize("failure", ["before", "after", "paused"])
async def test_resume_recovers_only_its_original_intent(install_environment, monkeypatch, failure):
    _, native = setup(install_environment, monkeypatch, paused=True, failure=failure)
    args = argparse.Namespace(command="resume")
    with pytest.raises(InstallationError if failure == "paused" else OSError):
        await deployment.dispatch(args, install_environment)
    pending = read_state(install_environment)
    assert pending["upgrade"]["phase"] == "resuming"
    assert "last_upgrade" not in pending
    assert not (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()
    original = pending["upgrade"]["resume_command"]

    result = await deployment.dispatch(args, install_environment)
    assert result["generation"] == 5 and not result["paused"]
    assert len(native.calls) == (1 if failure == "after" else 2)
    assert all(intent == original for _, intent in native.calls)
    completed = read_state(install_environment)
    assert "upgrade" not in completed and completed["last_upgrade"]["phase"] == "completed"
    assert (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()
    # An operator repeating the acknowledged resume must not increment its generation again.
    await deployment.dispatch(args, install_environment)
    assert native.status["generation"] == 5
    assert len(native.calls) == (1 if failure == "after" else 2)


@pytest.mark.parametrize("failure", ["before", "after", "timeout"])
async def test_interrupted_drain_can_continue_without_a_second_pause(install_environment, monkeypatch, failure):
    _, native = setup(install_environment, monkeypatch, paused=False, failure=failure)
    args = argparse.Namespace(command="begin-upgrade", revision="2" * 40, timeout=0 if failure == "timeout" else 1)
    with pytest.raises(TimeoutError if failure == "timeout" else OSError):
        await deployment.dispatch(args, install_environment)
    pending = read_state(install_environment)
    assert pending["phase"] == "initialized"
    assert pending["upgrade"]["phase"] == "requesting_drain"
    original = pending["upgrade"]["drain_command"]
    if failure == "timeout":
        native.status["state"] = "drained"
    args.timeout = 1
    await deployment.dispatch(args, install_environment)
    drained = read_state(install_environment)
    assert drained["phase"] == "upgrading" and drained["upgrade"]["phase"] == "drained"
    assert drained["upgrade"]["generation"] == 5
    assert len(native.calls) == (2 if failure == "before" else 1)
    assert all(intent == original for _, intent in native.calls)
    await deployment.dispatch(args, install_environment)
    assert native.status["generation"] == 5
    assert len(native.calls) == (2 if failure == "before" else 1)
    with pytest.raises(InstallationError, match="settled baseline"):
        await deployment.dispatch(argparse.Namespace(command="begin-upgrade", revision="3" * 40, timeout=1),
                                  install_environment)


@pytest.mark.parametrize("command", ["begin-upgrade", "resume"])
@pytest.mark.parametrize("drift", ["generation", "worker", "receipt_owner"])
async def test_recovery_refuses_changed_maintenance_authority(install_environment, monkeypatch, command, drift):
    _, native = setup(
        install_environment, monkeypatch, paused=command == "resume",
        failure="after" if drift == "receipt_owner" else "before",
    )
    args = argparse.Namespace(command=command, revision="2" * 40, timeout=1)
    with pytest.raises(OSError):
        await deployment.dispatch(args, install_environment)
    if drift == "generation":
        native.status.update(generation=9, request_key="somebody-else")
    elif drift == "worker":
        native.status["workers"][0]["pid"] = 99
    else:
        native.status["requester"]["pid"] = 99
    with pytest.raises(InstallationError, match="generation or worker changed"):
        await deployment.dispatch(args, install_environment)
    assert len(native.calls) == 1
    assert "upgrade" in read_state(install_environment)
    assert not (Path(install_environment["DATA_DIR"]) / "ingress/ready").exists()
