from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from moyai import fleet_admission, kubernetes_fleet
from moyai.deployment_config import InstallationError
from moyai.fleet_registry import FleetAdmissionError


def identity(monkeypatch, tmp_path):
    workload, pod = str(uuid4()), str(uuid4())
    monkeypatch.setenv("MOYAI_FLEET_PROVIDER", "kubernetes")
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("MOYAI_KUBERNETES_NAMESPACE", "moyai")
    monkeypatch.setenv("MOYAI_KUBERNETES_WORKLOAD_NAME", "green-api")
    monkeypatch.setenv("MOYAI_KUBERNETES_WORKLOAD_UID", workload)
    monkeypatch.setenv("MOYAI_KUBERNETES_POD_UID", pod)
    revision = tmp_path / "revision"
    revision.write_text("a" * 40)
    monkeypatch.setattr(fleet_admission, "BUILD_REVISION_FILE", revision)
    return workload, pod, revision


def test_pod_replacement_changes_membership_without_reusing_old_process_identity(monkeypatch, tmp_path):
    workload, pod, _ = identity(monkeypatch, tmp_path)
    old = fleet_admission.service_identity("a" * 40, local_rehearsal=False)
    assert old == (f"k8s-{workload}", f"k8s-{workload}-{pod}", "moyai/green-api")
    monkeypatch.setenv("MOYAI_KUBERNETES_POD_UID", str(uuid4()))
    new = fleet_admission.service_identity("a" * 40, local_rehearsal=False)
    assert new[0] == old[0] and new[1] != old[1]
    monkeypatch.setenv("MOYAI_KUBERNETES_WORKLOAD_UID", str(uuid4()))
    assert fleet_admission.service_identity("a" * 40, local_rehearsal=False)[0] != old[0]


@pytest.mark.parametrize("change", [
    {"MOYAI_KUBERNETES_POD_UID": ""},
    {"MOYAI_KUBERNETES_POD_UID": "api-7c4c-pod"},
    {"MOYAI_KUBERNETES_NAMESPACE": "../other"},
    {"RENDER_SERVICE_ID": "srv-1234567890abcdefghij"},
])
def test_ambiguous_or_partial_provider_identity_is_rejected(monkeypatch, tmp_path, change):
    identity(monkeypatch, tmp_path)
    for key, value in change.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(FleetAdmissionError):
        fleet_admission.service_identity("a" * 40, local_rehearsal=False)


def test_kubernetes_still_checks_the_embedded_image_revision(monkeypatch, tmp_path):
    _, _, revision = identity(monkeypatch, tmp_path)
    revision.write_text("b" * 40)
    with pytest.raises(FleetAdmissionError, match="image_revision_changed"):
        fleet_admission.service_identity("a" * 40, local_rehearsal=False)


def snapshot(tmp_path, **changes):
    state = {
        "version": 1, "scope": "scope", "phase": "initialized", "revision": "a" * 40,
        "activation_pending": False,
        "secrets": dict.fromkeys((
            "WORKSPACE_PASSWORD", "SESSION_SECRET", "ENCRYPTION_KEY", "MOYAI_NATIVE_ENCRYPTION_KEY",
        ), "private-test-key"),
        **changes,
    }
    source = tmp_path / "projected-secret"
    source.write_text(json.dumps(state))
    return source


def test_secret_snapshot_gets_a_private_copy_and_cannot_overwrite_runtime_state(monkeypatch, tmp_path):
    source = snapshot(tmp_path)
    projected = tmp_path / "installation-secret"
    projected.symlink_to(source)
    destination = tmp_path / "runtime"
    monkeypatch.setenv("DATA_DIR", str(destination))
    kubernetes_fleet.prepare(projected)
    copied = destination / "installation.json"
    assert copied.read_bytes() == source.read_bytes()
    assert not copied.is_symlink() and copied.stat().st_mode & 0o777 == 0o600
    source.write_text("{}")
    assert json.loads(copied.read_text())["phase"] == "initialized"
    before = copied.read_bytes()
    with pytest.raises(FileExistsError):
        kubernetes_fleet.prepare(projected)
    assert copied.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"activation_pending": True}, {"phase": "upgrading"}, {"upgrade": {"phase": "drained"}},
])
def test_unfinished_installation_cannot_be_copied_into_a_fleet(monkeypatch, tmp_path, changes):
    source = snapshot(tmp_path, **changes)
    destination = tmp_path / "runtime"
    monkeypatch.setenv("DATA_DIR", str(destination))
    with pytest.raises(InstallationError):
        kubernetes_fleet.prepare(source)
    assert not (destination / "installation.json").exists()


def test_copy_rejects_a_shared_or_symlinked_runtime_directory(monkeypatch, tmp_path):
    source = snapshot(tmp_path)
    destination = tmp_path / "runtime"
    destination.mkdir(mode=0o755)
    os.chmod(destination, 0o755)
    monkeypatch.setenv("DATA_DIR", str(destination))
    with pytest.raises(InstallationError, match="permissions"):
        kubernetes_fleet.prepare(source)
