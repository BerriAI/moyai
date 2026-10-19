from __future__ import annotations

from types import SimpleNamespace

import pytest

from moyai.fleet_admission import FleetAdmission
from moyai.fleet_registry import FleetAdmissionError


def life():
    return SimpleNamespace(runtime=SimpleNamespace(
        product=SimpleNamespace(settings=SimpleNamespace(moyai_build_sha="a" * 40)),
    ))


@pytest.mark.parametrize("actual", [None, "", "b" * 40, "unqualified"])
def test_render_build_label_cannot_hide_a_different_image(monkeypatch, tmp_path, actual):
    from moyai import fleet_admission

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-1234567890abcdefghij")
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    revision = tmp_path / "build-revision"
    monkeypatch.setattr(fleet_admission, "BUILD_REVISION_FILE", revision)
    if actual is not None:
        revision.write_text(actual)
    with pytest.raises(FleetAdmissionError, match="image_revision"):
        FleetAdmission(life())


@pytest.mark.parametrize("git_commit", [None, "", "a" * 40])
def test_prebuilt_images_do_not_require_a_render_git_commit(monkeypatch, tmp_path, git_commit):
    from moyai import fleet_admission

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-1234567890abcdefghij")
    if git_commit is None:
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("RENDER_GIT_COMMIT", git_commit)
    revision = tmp_path / "build-revision"
    revision.write_text("a" * 40)
    monkeypatch.setattr(fleet_admission, "BUILD_REVISION_FILE", revision)
    monkeypatch.delenv("MOYAI_FLEET_REQUIRED", raising=False)
    assert FleetAdmission(life()).required is False


def test_git_backed_image_also_rejects_a_conflicting_platform_commit(monkeypatch, tmp_path):
    from moyai import fleet_admission

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-1234567890abcdefghij")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "b" * 40)
    revision = tmp_path / "build-revision"
    revision.write_text("a" * 40)
    monkeypatch.setattr(fleet_admission, "BUILD_REVISION_FILE", revision)
    with pytest.raises(FleetAdmissionError, match="image_revision_changed"):
        FleetAdmission(life())


def test_registry_cannot_be_redirected_to_an_absent_schema(monkeypatch):
    monkeypatch.setenv("MOYAI_FLEET_REGISTRY_SCHEMA", "moyai_fleet_admission_missing")
    with pytest.raises(FleetAdmissionError, match="cannot_be_redirected"):
        FleetAdmission(life())


def test_process_quiesce_cannot_bypass_or_rename_a_durable_service_seal(monkeypatch):
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    admission = FleetAdmission(life())
    admission.registered = True
    admission.current = {"state": "accepting", "drain_request": ""}
    with pytest.raises(FleetAdmissionError, match="requires_durable"):
        admission.require_drain("release-one")
    admission.current = {"state": "draining", "drain_request": "release-one"}
    admission.require_drain("release-one")
    with pytest.raises(FleetAdmissionError, match="requires_durable"):
        admission.require_drain("different-release")


@pytest.mark.parametrize("local", [True, False])
def test_synthetic_provider_identity_is_scoped_to_explicit_rehearsals(monkeypatch, local):
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setenv("MOYAI_LOCAL_FLEET_SERVICE_ID", "srv-1234567890abcdefghij")
    monkeypatch.setenv("MOYAI_LOCAL_FLEET_INSTANCE_ID", "srv-1234567890abcdefghij-abcde")
    holder = life()
    holder.runtime.product_config = SimpleNamespace(local_rehearsal=local)
    admission = FleetAdmission(holder)
    assert bool(admission.service_id) is local
