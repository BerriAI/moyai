"""Exercise prerelease gating, package boundaries, and publication retry safety."""

import base64
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import build_docker_bundle, build_helm_bundle
from scripts import dev_release as dev

REVISION = "a" * 40
DATE = "2026-11-05T03:41:22Z"
VERSION = "0.0.0-dev.20261105034122.gaaaaaaaaaaaa"
DIGEST = "sha256:" + "b" * 64


def passing_runs():
    return [
        {
            "id": index,
            "run_attempt": 1,
            "head_sha": REVISION,
            "head_branch": "main",
            "event": "push",
            "repository": {"full_name": dev.REPOSITORY},
            "path": f".github/workflows/{name}",
            "status": "completed",
            "conclusion": "success",
        }
        for index, name in enumerate(sorted(dev.REQUIRED), 1)
    ]


def test_version_is_deterministic_and_sorts_by_commit_time():
    assert dev.dev_version(REVISION, DATE) == VERSION
    assert dev.dev_version(REVISION, "2026-11-05T11:41:22+08:00") == VERSION
    assert dev.dev_version(REVISION, "2026-11-05T04:41:22Z") > VERSION
    with pytest.raises(ValueError):
        dev.dev_version("main", DATE)


@pytest.mark.parametrize(
    "change",
    [
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"status": "in_progress", "conclusion": None},
        {"head_sha": "b" * 40},
        {"head_branch": "other"},
        {"event": "pull_request"},
        {"repository": {"full_name": "fork/moyai"}},
    ],
)
def test_required_ci_cannot_be_replaced_by_unverified_checks(change):
    runs = passing_runs()
    assert dev.ci_failures(runs, REVISION) == []
    runs[0].update(change)
    assert dev.ci_failures(runs, REVISION)


def test_latest_attempt_and_triggered_optional_checks_must_pass():
    runs = passing_runs()
    retry = {**runs[0], "run_attempt": 2, "conclusion": "failure"}
    assert dev.ci_failures([retry, *runs], REVISION)
    optional = {
        **runs[0],
        "path": ".github/workflows/helm.yml",
        "conclusion": "failure",
    }
    assert dev.ci_failures([*runs, optional], REVISION) == ["helm.yml"]
    assert dev.ci_failures(runs[1:], REVISION)


@pytest.mark.parametrize(
    "name,event,branch,expected",
    [
        ("docker.yml", "push", "main", True),
        ("docker.yml", "pull_request", "feature", False),
        ("docker-release.yml", "release", "v0.1.5", True),
        ("dev-release.yml", "schedule", "main", True),
        ("dev-release.yml", "pull_request", "feature", False),
    ],
)
def test_busy_publication_skips_but_pr_checks_do_not(name, event, branch, expected):
    item = {
        "id": 42,
        "path": f".github/workflows/{name}",
        "event": event,
        "head_branch": branch,
        "status": "queued",
    }
    assert dev.busy([item], "43") is expected
    assert dev.busy([item], "42") is False
    assert dev.busy([{**item, "status": "completed"}], "43") is False


def prepare_gate(monkeypatch, *, head=REVISION, release=None, tag=None, active=()):
    def api(path, **kwargs):
        if path == "commits/main":
            return {"sha": head, "commit": {"committer": {"date": DATE}}}
        if path.startswith("git/ref/tags/"):
            return tag
        raise AssertionError(path)

    runs = Mock(
        side_effect=lambda query: (
            active if query.startswith("status=") else passing_runs()
        )
    )
    monkeypatch.setattr(dev, "api", api)
    monkeypatch.setattr(dev, "release_for_tag", Mock(return_value=release))
    monkeypatch.setattr(dev, "runs", runs)
    return runs


def test_release_lookup_recovers_unpublished_draft_by_pending_tag(monkeypatch):
    draft = {"draft": True, "target_commitish": REVISION, "id": 123}
    monkeypatch.setattr(
        dev,
        "run",
        Mock(
            return_value=json.dumps(
                {"data": {"repository": {"release": {"databaseId": 123}}}}
            )
        ),
    )
    request = Mock(return_value=draft)
    monkeypatch.setattr(dev, "api", request)
    assert dev.release_for_tag("v" + VERSION) == draft
    request.assert_called_once_with("releases/123")
    monkeypatch.setattr(
        dev,
        "run",
        Mock(return_value=json.dumps({"data": {"repository": {"release": None}}})),
    )
    assert dev.release_for_tag("v" + VERSION) is None


def test_gate_selects_ready_head_and_skips_unchanged_or_advanced_main(monkeypatch):
    prepare_gate(monkeypatch)
    assert dev.gate(REVISION) == (VERSION, None)
    runs = prepare_gate(monkeypatch, head="b" * 40)
    assert dev.gate(REVISION) is None
    runs.assert_not_called()
    runs = prepare_gate(
        monkeypatch,
        release={"target_commitish": REVISION, "prerelease": True, "draft": False},
    )
    assert dev.gate(REVISION) is None
    runs.assert_not_called()


def test_gate_resumes_only_matching_drafts_and_rejects_wrong_tags(monkeypatch):
    draft = {"target_commitish": REVISION, "prerelease": True, "draft": True}
    prepare_gate(monkeypatch, release=draft)
    assert dev.gate(REVISION) == (VERSION, draft)
    prepare_gate(monkeypatch, release={**draft, "target_commitish": "main"})
    with pytest.raises(ValueError, match="source revision"):
        dev.gate(REVISION)
    prepare_gate(monkeypatch, tag={"object": {"type": "commit", "sha": "b" * 40}})
    with pytest.raises(ValueError, match="dev tag"):
        dev.gate(REVISION)


def test_gate_skips_active_build(monkeypatch):
    active = [
        {
            **passing_runs()[0],
            "path": ".github/workflows/docker.yml",
            "status": "in_progress",
        }
    ]
    prepare_gate(monkeypatch, active=active)
    assert dev.gate(REVISION) is None


@pytest.fixture
def packages(tmp_path):
    bundle = tmp_path / "moyai-docker.tar.gz"
    build_docker_bundle.build(
        ROOT / "deploy/docker", bundle, "ghcr.io/berriai/moyai@" + DIGEST, REVISION
    )
    build_helm_bundle.build(ROOT / "charts/moyai", tmp_path, DIGEST, REVISION, VERSION)
    return bundle, tmp_path / f"moyai-{VERSION}.tgz"


def write_archive(path, files):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def test_real_packages_use_dev_version_and_matching_image(packages):
    bundle, chart = packages
    files = dev.archive_files(chart)
    metadata = yaml.safe_load(files["moyai/Chart.yaml"])
    assert metadata["version"] == VERSION
    assert metadata["appVersion"] == REVISION
    assert yaml.safe_load(files["moyai/values.yaml"])["image"]["digest"] == DIGEST
    dev.scan_artifacts(bundle, chart, ROOT)
    assert (
        yaml.safe_load((ROOT / "charts/moyai/Chart.yaml").read_text())["version"]
        != VERSION
    )


@pytest.mark.parametrize(
    "encoding", [str.encode, lambda value: base64.b64encode(value.encode())]
)
def test_actual_environment_credentials_are_rejected_without_logging_them(
    packages, monkeypatch, encoding
):
    bundle, chart = packages
    secret = "synthetic-only-credential-for-this-test"
    monkeypatch.setenv("GH_TOKEN", secret)
    files = dev.archive_files(bundle)
    files["moyai-docker/README.md"] += encoding(secret)
    write_archive(bundle, files)
    with pytest.raises(ValueError, match="Credential material") as error:
        dev.scan_artifacts(bundle, chart, ROOT)
    assert secret not in str(error.value)


def test_unexpected_files_and_token_patterns_are_rejected(packages):
    bundle, chart = packages
    files = dev.archive_files(bundle)
    write_archive(bundle, {**files, "moyai-docker/.env": b"DO_NOT_UPLOAD=true"})
    with pytest.raises(ValueError, match="allowlist"):
        dev.scan_artifacts(bundle, chart, ROOT)
    files["moyai-docker/README.md"] += b"ghp_" + b"x" * 40
    write_archive(bundle, files)
    with pytest.raises(ValueError, match="Credential material"):
        dev.scan_artifacts(bundle, chart, ROOT)


@pytest.mark.parametrize(
    "name,kind", [("../outside", tarfile.REGTYPE), ("moyai/link", tarfile.SYMTYPE)]
)
def test_archives_reject_paths_and_links(tmp_path, name, kind):
    path = tmp_path / "unsafe.tgz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "outside"
        archive.addfile(member)
    with pytest.raises(ValueError):
        dev.archive_files(path)


def test_publish_keeps_release_draft_until_chart_and_assets_succeed(
    packages, monkeypatch
):
    bundle, chart = packages
    draft = {"id": 123, "target_commitish": REVISION, "draft": True, "prerelease": True}
    api = Mock(return_value=draft)
    monkeypatch.setattr(dev, "api", api)
    monkeypatch.setattr(
        dev, "publish_chart", Mock(side_effect=RuntimeError("registry unavailable"))
    )
    with pytest.raises(RuntimeError):
        dev.publish_release(REVISION, VERSION, None, bundle, chart, ROOT)
    create = api.call_args_list[0].kwargs["data"]
    assert create["draft"] and create["prerelease"] and create["make_latest"] == "false"
    assert not any(call.kwargs.get("method") == "PATCH" for call in api.call_args_list)
    monkeypatch.setattr(dev, "publish_chart", Mock())
    monkeypatch.setattr(dev.subprocess, "run", Mock())
    # Keep the source allowlist command real while replacing only the upload.
    monkeypatch.setattr(dev, "scan_artifacts", Mock())
    dev.publish_release(REVISION, VERSION, draft, bundle, chart, ROOT)
    assert api.call_args.kwargs["data"] == {
        "draft": False,
        "prerelease": True,
        "make_latest": "false",
    }


def test_dev_range_excludes_stable_and_other_prereleases(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: range-test\nversion: 0.1.0\n"
    )
    (tmp_path / "templates/result.yaml").write_text("""data:
{{- range $v := list "0.0.0-dev.20261105034122.gaaaaaaaaaaaa" "0.0.0" "0.1.5" "0.0.0-rc.1" "0.0.0-beta.1" }}
  {{ $v }}: {{ semverCompare ">=0.0.0-dev.0 <0.0.0-dev.z" $v | quote }}
{{- end }}
""")
    data = yaml.safe_load(subprocess.check_output(["helm", "template", str(tmp_path)]))[
        "data"
    ]
    assert data.pop(VERSION) == "true"
    assert set(data.values()) == {"false"}
