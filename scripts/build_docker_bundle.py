"""Create a release bundle from an explicit deployment-file allowlist."""

from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
from pathlib import Path

FILES = (
    ".env.example", "README.md", "compose.common.yaml", "compose.yaml",
    "compose.split.yaml", "compose.temporal.yaml", "nginx.standalone.conf",
    "nginx.split.conf", "temporal-databases.sql", "temporal-dynamic.yaml", "temporal-schema.sh", "upgrade.sh",
)


def build(source: Path, output: Path, image: str, revision: str) -> None:
    if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Supply an immutable image digest.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Supply the full release revision.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for name in FILES:
            path = source / name
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"Missing release input: {name}")
            content = path.read_bytes()
            if name == ".env.example":
                content = content.replace(b"MOYAI_IMAGE=\n", f"MOYAI_IMAGE={image}\n".encode(), 1)
            info = tarfile.TarInfo(f"moyai-docker/{name}")
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(content))
        for name, value in (
            ("image.txt", image + "\n"),
            ("release.json", json.dumps({"image": image, "revision": revision}, sort_keys=True) + "\n"),
        ):
            content = value.encode()
            info = tarfile.TarInfo(f"moyai-docker/{name}")
            info.size, info.mode = len(content), 0o644
            archive.addfile(info, io.BytesIO(content))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1] / "deploy/docker", args.output, args.image, args.revision)


if __name__ == "__main__":
    main()
