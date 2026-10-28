"""Resolve a public, smoke-tested main image without rebuilding it for a release."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

REPOSITORY = "BerriAI/moyai"
IMAGE = "ghcr.io/berriai/moyai"
ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}


def verify_ci(revision: str) -> None:
    """Require the latest main-push Docker run, including publication, to pass."""
    result = subprocess.run(
        [
            "gh",
            "api",
            (
                f"repos/{REPOSITORY}/actions/workflows/docker.yml/runs"
                f"?branch=main&event=push&head_sha={revision}&per_page=1"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    runs = json.loads(result.stdout)["workflow_runs"]
    if not runs or not all(
        [
            runs[0]["head_sha"] == revision,
            runs[0]["head_branch"] == "main",
            runs[0]["event"] == "push",
            runs[0]["status"] == "completed",
            runs[0]["conclusion"] == "success",
        ]
    ):
        raise ValueError(
            "The latest Docker startup main-push run for this revision must succeed first"
        )


class Registry:
    """Anonymous GHCR reads; never use the publishing credential to prove access."""

    def __init__(self) -> None:
        with urllib.request.urlopen(
            "https://ghcr.io/token?service=ghcr.io&scope=repository:berriai/moyai:pull",
            timeout=30,
        ) as response:
            self.token = json.load(response)["token"]

    def read(self, kind: str, reference: str) -> tuple[dict, str]:
        request = urllib.request.Request(
            f"https://ghcr.io/v2/berriai/moyai/{kind}/{reference}",
            headers={"Authorization": "Bearer " + self.token, "Accept": ACCEPT},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if reference.startswith("sha256:") and reference != digest:
            raise ValueError(
                f"Registry content does not match requested digest {reference}"
            )
        return json.loads(data), digest


def verify_image(registry: Registry, reference: str, revision: str) -> str:
    index, digest = registry.read("manifests", reference)
    seen = set()
    for entry in index["manifests"]:
        platform = entry.get("platform", {})
        pair = (platform.get("os"), platform.get("architecture"))
        if pair not in PLATFORMS:
            continue  # Preserve attestation descriptors when promoting the entire index.
        if pair in seen:
            raise ValueError(f"Duplicate runnable platform: {pair}")
        manifest, _ = registry.read("manifests", entry["digest"])
        config, _ = registry.read("blobs", manifest["config"]["digest"])
        if (config["os"], config["architecture"]) != pair:
            raise ValueError(f"Image config does not match index platform: {pair}")
        if (
            config["config"].get("Labels", {}).get("org.opencontainers.image.revision")
            != revision
        ):
            raise ValueError(f"Image revision does not match release revision: {pair}")
        seen.add(pair)
    if seen != PLATFORMS:
        raise ValueError("Release image must contain both linux/amd64 and linux/arm64")
    return digest


def check_release_tag(registry: Registry, revision: str, digest: str) -> None:
    """Allow a retry of the same image, but never replace a different release image."""
    try:
        _, existing = registry.read("manifests", f"sha-{revision}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    if existing != digest:
        raise ValueError(
            "Release image tag already exists with a different digest; refusing to replace it"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--check-release-tag", action="store_true")
    parser.add_argument("--verify-promoted-digest")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be a full lowercase Git SHA")
    if args.verify_promoted_digest and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", args.verify_promoted_digest
    ):
        parser.error("promoted digest must be a SHA256 digest")
    verify_ci(args.revision)
    registry = Registry()
    if args.verify_promoted_digest:
        digest = verify_image(registry, f"sha-{args.revision}", args.revision)
        if digest != args.verify_promoted_digest:
            raise ValueError("Promoted tag does not match the verified source digest")
    else:
        digest = verify_image(
            registry, f"quickstart-sha-{args.revision}", args.revision
        )
        if args.check_release_tag:
            check_release_tag(registry, args.revision, digest)
    print(f"Verified {IMAGE}@{digest} for {args.revision}: linux/amd64 and linux/arm64")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.write(f"digest={digest}\n")


if __name__ == "__main__":
    main()
