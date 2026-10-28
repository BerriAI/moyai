import copy
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from scripts import verify_release_image as release

REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64


def test_requires_successful_latest_main_push_for_exact_revision(monkeypatch):
    run = {
        "head_sha": REVISION,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    result = {"workflow_runs": [run]}

    def api(command, **kwargs):
        assert command[-1].endswith(
            f"branch=main&event=push&head_sha={REVISION}&per_page=1"
        )
        return SimpleNamespace(stdout=json.dumps(result))

    monkeypatch.setattr(release.subprocess, "run", api)
    release.verify_ci(REVISION)
    for field, value in [
        ("head_sha", "c" * 40),
        ("head_branch", "feature"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
    ]:
        result["workflow_runs"] = [{**run, field: value}]
        with pytest.raises(ValueError, match="must succeed first"):
            release.verify_ci(REVISION)
    result["workflow_runs"] = []
    with pytest.raises(ValueError, match="must succeed first"):
        release.verify_ci(REVISION)


@pytest.fixture
def registry():
    documents = {
        "image": {
            "manifests": [
                {"digest": arch, "platform": {"os": "linux", "architecture": arch}}
                for arch in ["amd64", "arm64", "unknown"]
            ]
        },
    }
    for arch in ["amd64", "arm64"]:
        documents[arch] = {"config": {"digest": f"{arch}-config"}}
        documents[f"{arch}-config"] = {
            "os": "linux",
            "architecture": arch,
            "config": {"Labels": {"org.opencontainers.image.revision": REVISION}},
        }
    return SimpleNamespace(
        read=lambda kind, ref: (documents[ref], DIGEST), documents=documents
    )


def test_resolves_both_architectures_and_ignores_attestation_platform(registry):
    assert release.verify_image(registry, "image", REVISION) == DIGEST


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_rejects_wrong_revision_on_either_architecture(registry, arch):
    registry.documents[f"{arch}-config"]["config"]["Labels"][
        "org.opencontainers.image.revision"
    ] = "c" * 40
    with pytest.raises(ValueError, match="revision does not match"):
        release.verify_image(registry, "image", REVISION)


def test_rejects_missing_architecture(registry):
    registry.documents["image"]["manifests"].pop(1)
    with pytest.raises(ValueError, match="both linux"):
        release.verify_image(registry, "image", REVISION)


def test_rejects_duplicate_platform(registry):
    registry.documents["image"]["manifests"].append(
        copy.deepcopy(registry.documents["image"]["manifests"][0])
    )
    with pytest.raises(ValueError, match="Duplicate"):
        release.verify_image(registry, "image", REVISION)


def test_rejects_mislabeled_platform(registry):
    registry.documents["arm64-config"]["architecture"] = "amd64"
    with pytest.raises(ValueError, match="config does not match"):
        release.verify_image(registry, "image", REVISION)


@pytest.mark.parametrize("code", [401, 403, 500])
def test_registry_errors_do_not_mean_release_tag_is_available(code):
    def read(*args):
        raise HTTPError("https://ghcr.io", code, "unavailable", {}, None)

    with pytest.raises(HTTPError):
        release.check_release_tag(SimpleNamespace(read=read), REVISION, DIGEST)


def test_release_tag_is_new_identical_or_rejected():
    def absent(*args):
        raise HTTPError("https://ghcr.io", 404, "missing", {}, None)

    release.check_release_tag(SimpleNamespace(read=absent), REVISION, DIGEST)
    release.check_release_tag(
        SimpleNamespace(read=lambda *args: ({}, DIGEST)), REVISION, DIGEST
    )
    with pytest.raises(ValueError, match="refusing to replace"):
        release.check_release_tag(
            SimpleNamespace(read=lambda *args: ({}, "sha256:" + "c" * 64)),
            REVISION,
            DIGEST,
        )


def test_registry_verifies_content_digest(monkeypatch):
    import io

    monkeypatch.setattr(
        release.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(b'{"token":"anonymous"}'),
    )
    registry = release.Registry()
    with pytest.raises(ValueError, match="does not match requested digest"):
        registry.read("blobs", DIGEST)
