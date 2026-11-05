"""Publish an hourly development prerelease from an already verified main image."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import build_docker_bundle, build_helm_bundle, verify_release_image

REPOSITORY = "BerriAI/moyai"
CHART = "oci://ghcr.io/berriai/charts/moyai"
REQUIRED = {
    "docker.yml",
    "frontend.yml",
    "moyai-evals.yml",
    "postgres-runtime.yml",
    "production-release-tests.yml",
    "pydantic-native.yml",
    "ui-unit.yml",
}
ACTIVE = ("in_progress", "queued", "pending", "waiting", "requested")
SECRET_PATTERN = re.compile(
    rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{20,}"
)


def run(*arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, **kwargs).strip()


def api(path, *, method="GET", data=None, missing=False):
    command = ["gh", "api", "--method", method, f"repos/{REPOSITORY}/{path}"]
    if data is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if missing and result.returncode and "(HTTP 404)" in result.stderr:
        return None
    if result.returncode:
        # Never include arbitrary API responses or credential-bearing command output.
        raise RuntimeError(f"GitHub request failed: {method} {path}")
    return json.loads(result.stdout)


def runs(query):
    pages = json.loads(
        run(
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{REPOSITORY}/actions/runs?per_page=100&{query}",
        )
    )
    return [item for page in pages for item in page["workflow_runs"]]


def busy(workflows, own_run):
    for item in workflows:
        if str(item["id"]) == own_run or item["status"] == "completed":
            continue
        name = item["path"].removeprefix(".github/workflows/")
        main_build = (
            name == "docker.yml"
            and item["head_branch"] == "main"
            and item["event"] in {"push", "workflow_dispatch"}
        )
        publication = name == "docker-release.yml" or (
            name == "dev-release.yml"
            and item["event"] in {"schedule", "workflow_dispatch"}
        )
        if main_build or publication:
            return True
    return False


def ci_failures(workflows, revision):
    latest = {}
    for item in workflows:
        if (
            item["head_sha"] != revision
            or item["head_branch"] != "main"
            or item["event"] != "push"
            or item["repository"]["full_name"] != REPOSITORY
        ):
            continue
        name = item["path"].removeprefix(".github/workflows/")
        previous = latest.get(name)
        if previous is None or (item["id"], item["run_attempt"]) > (
            previous["id"],
            previous["run_attempt"],
        ):
            latest[name] = item
    failures = sorted(REQUIRED - latest.keys())
    failures.extend(
        name
        for name, item in latest.items()
        if item["status"] != "completed" or item["conclusion"] != "success"
    )
    return failures


def dev_version(revision, committed_at):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected a full source revision.")
    timestamp = datetime.fromisoformat(committed_at)
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    return f"0.0.0-dev.{stamp}.g{revision[:12]}"


def gate(revision):
    head = api("commits/main")
    if head["sha"] != revision:
        print(
            "Skip: main advanced; the next hourly run will check the new revision.",
            flush=True,
        )
        return None
    version = dev_version(revision, head["commit"]["committer"]["date"])
    tag = "v" + version
    existing_tag = api(f"git/ref/tags/{tag}", missing=True)
    if existing_tag and (
        existing_tag["object"]["type"] != "commit"
        or existing_tag["object"]["sha"] != revision
    ):
        raise ValueError(
            "Existing dev tag does not point to the selected source revision."
        )
    release = api(f"releases/tags/{tag}", missing=True)
    if release:
        if release["target_commitish"] != revision or not release["prerelease"]:
            raise ValueError(
                "Existing dev release does not match the selected source revision."
            )
        if not release["draft"]:
            print(f"Skip: {tag} already published; no new changes.", flush=True)
            return None
    own_run = os.environ.get("GITHUB_RUN_ID", "")
    if any(busy(runs(f"status={status}"), own_run) for status in ACTIVE):
        print("Skip: a main image build or release is already active.", flush=True)
        return None
    failures = ci_failures(runs(f"event=push&head_sha={revision}"), revision)
    if failures:
        print(
            f"Skip: required main CI is not green ({', '.join(sorted(failures))}).",
            flush=True,
        )
        return None
    print(
        f"Ready: {tag}; exact-commit main CI passed and publishers are idle.",
        flush=True,
    )
    return version, release


def archive_files(path):
    result = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile() or member.name in result:
                raise ValueError("Release archive contains links or duplicate files.")
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise ValueError("Release archive contains an unsafe path.")
            result[member.name] = archive.extractfile(member).read()
    return result


def scan_artifacts(bundle, chart, source):
    expected_bundle = {
        f"moyai-docker/{name}"
        for name in (*build_docker_bundle.FILES, "image.txt", "release.json")
    }
    # The chart may contain only files from its tracked source directory.
    tracked = run("git", "-C", str(source), "ls-files", "charts/moyai").splitlines()
    expected_chart = {
        "moyai/" + str(Path(name).relative_to("charts/moyai")) for name in tracked
    }
    secrets = []
    for name, value in os.environ.items():
        if len(value) >= 12 and re.search(
            r"TOKEN|SECRET|PASSWORD|PRIVATE_KEY|API_KEY", name, re.IGNORECASE
        ):
            secrets.extend(
                [
                    value.encode(),
                    base64.b64encode(value.encode()),
                    urllib.parse.quote(value, safe="").encode(),
                ]
            )
    for path, allowed in ((bundle, expected_bundle), (chart, expected_chart)):
        files = archive_files(path)
        if path == bundle and files.keys() != allowed:
            raise ValueError(
                "Docker bundle does not match the deployment-file allowlist."
            )
        if not files or not files.keys() <= allowed:
            raise ValueError(
                "Release archive contains files outside its source allowlist."
            )
        for name, content in files.items():
            if (
                Path(name).name == ".env"
                or SECRET_PATTERN.search(content)
                or any(value in content for value in secrets)
            ):
                raise ValueError(
                    "Credential material detected; refusing to upload release artifacts."
                )
    print(
        "Artifact scan passed: allowlisted files only; no detected credential material.",
        flush=True,
    )


def chart_exists(version):
    with urllib.request.urlopen(
        "https://ghcr.io/token?service=ghcr.io&scope=repository:berriai/charts/moyai:pull",
        timeout=30,
    ) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/berriai/charts/moyai/manifests/{version}",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.manifest.v1+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def publish_chart(chart, version):
    with tempfile.TemporaryDirectory(prefix="moyai-registry-") as temporary:
        root = Path(temporary)
        (root / "docker").mkdir()
        public_env = {
            **os.environ,
            "HELM_REGISTRY_CONFIG": str(root / "anonymous.json"),
            "DOCKER_CONFIG": str(root / "docker"),
        }
        if not chart_exists(version):
            private_env = {
                **os.environ,
                "HELM_REGISTRY_CONFIG": str(root / "publisher.json"),
            }
            subprocess.run(
                [
                    "helm",
                    "registry",
                    "login",
                    "ghcr.io",
                    "--username",
                    os.environ["GITHUB_ACTOR"],
                    "--password-stdin",
                ],
                input=os.environ["GH_TOKEN"],
                text=True,
                check=True,
                capture_output=True,
                env=private_env,
            )
            subprocess.run(
                ["helm", "push", str(chart), "oci://ghcr.io/berriai/charts"],
                check=True,
                env=private_env,
            )
        subprocess.run(
            ["helm", "pull", CHART, "--version", version, "--destination", str(root)],
            check=True,
            env=public_env,
        )
        published = root / chart.name
        if archive_files(published) != archive_files(chart):
            raise ValueError(
                "Published dev chart differs from this source; refusing to overwrite it."
            )
        # Attach the exact OCI bytes, including on a retry after a partial publication.
        chart.write_bytes(published.read_bytes())
    print("Verified anonymous access to the matching immutable dev chart.", flush=True)


def publish_release(revision, version, release, bundle, chart, source):
    tag = "v" + version
    scan_artifacts(bundle, chart, source)
    if release is None:
        release = api(
            "releases",
            method="POST",
            data={
                "tag_name": tag,
                "target_commitish": revision,
                "name": f"Dev {version.split('.')[3]} ({revision[:12]})",
                "body": f"Automated development prerelease of `{revision}`. Not a stable release.",
                "draft": True,
                "prerelease": True,
                "make_latest": "false",
            },
        )
    # GITHUB_TOKEN release events do not launch another workflow. Publication is
    # completed here, then the draft is made visible only after its chart is ready.
    publish_chart(chart, version)
    scan_artifacts(bundle, chart, source)
    current = api(f"releases/{release['id']}")
    if not current["draft"] or current["target_commitish"] != revision:
        raise ValueError(
            "Release changed during publication; refusing to replace public assets."
        )
    subprocess.run(
        [
            "gh",
            "release",
            "upload",
            tag,
            str(bundle),
            str(chart),
            "--repo",
            REPOSITORY,
            "--clobber",
        ],
        check=True,
    )
    api(
        f"releases/{release['id']}",
        method="PATCH",
        data={
            "draft": False,
            "prerelease": True,
            "make_latest": "false",
        },
    )
    print(
        f"Published dev prerelease {tag}; stable releases remain unchanged.", flush=True
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("release/dev"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    if args.publish and run("git", "-C", str(source), "status", "--porcelain"):
        raise ValueError("Publication requires a clean source checkout.")
    revision = run("git", "-C", str(source), "rev-parse", "HEAD")
    selected = gate(revision)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write(f"ready={'true' if selected else 'false'}\n")
    if not selected or args.check:
        return
    version, release = selected
    verify_release_image.verify_ci(revision)
    registry = verify_release_image.Registry()
    digest = verify_release_image.verify_image(
        registry, f"quickstart-sha-{revision}", revision
    )
    print(
        f"Verified public AMD64/ARM64 image {verify_release_image.IMAGE}@{digest}.",
        flush=True,
    )
    output = args.output.resolve()
    bundle = output / "moyai-docker.tar.gz"
    build_docker_bundle.build(
        source / "deploy/docker",
        bundle,
        f"{verify_release_image.IMAGE}@{digest}",
        revision,
    )
    build_helm_bundle.build(source / "charts/moyai", output, digest, revision, version)
    chart = output / f"moyai-{version}.tgz"
    for profile in ("bundled", "external"):
        values = source / f"charts/moyai/ci/{profile}.yaml"
        subprocess.run(
            ["helm", "lint", "--strict", str(chart), "-f", str(values)], check=True
        )
        manifests = run(
            "helm",
            "template",
            "moyai",
            str(chart),
            "-f",
            str(values),
            "--kube-version",
            "1.34.0",
        )
        (output / f"{profile}-rendered.yaml").write_text(manifests + "\n")
    scan_artifacts(bundle, chart, source)
    if not args.publish:
        print(
            f"Dry run: packaged v{version}; no uploads or releases created.", flush=True
        )
        return
    # The shared Actions lock closes the build/publication race; this second
    # gate also skips newly queued builds and changed main/CI before any writes.
    selected = gate(revision)
    if selected is not None:
        version, release = selected
        publish_release(revision, version, release, bundle, chart, source)


if __name__ == "__main__":
    main()
