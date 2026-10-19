"""A reproducible Node runtime shared by fresh and restored coding computers."""

from __future__ import annotations

import shlex

import modal

# Official https://nodejs.org/dist/v22.23.3/SHASUMS256.txt.
# Debian's Node 18 cannot run the repository's current Vite toolchain.
NODE_VERSION = "22.23.3"
NODE_SHA256 = {
    "amd64": "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de",
    "arm64": "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f",
}


def node_install_script() -> str:
    """One installer for Modal images and the packaged Lambda image."""
    return f"""#!/bin/sh
set -eu
case "$(dpkg --print-architecture)" in
  amd64) arch=x64; checksum={NODE_SHA256['amd64']} ;;
  arm64) arch=arm64; checksum={NODE_SHA256['arm64']} ;;
  *) echo 'Unsupported Node architecture' >&2; exit 1 ;;
esac
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
curl --fail --show-error --location "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$arch.tar.xz" -o "$archive"
printf '%s  %s\\n' "$checksum" "$archive" | sha256sum --check -
tar -xJf "$archive" --no-same-owner --strip-components=1 -C /usr/local
test "$(node --version)" = "v{NODE_VERSION}"
npm --version
# Reject incompatible dependencies during installation, before a preview can
# run with optional native packages silently omitted by npm.
npm config set --global engine-strict true
"""


def with_node(image: modal.Image) -> modal.Image:
    # Modal emits each command as a Dockerfile RUN instruction. Literal newlines
    # in the shell script would become separate, invalid Dockerfile instructions.
    command = (
        "import subprocess; "
        f"subprocess.run(['sh'], input={node_install_script()!r}, text=True, check=True)"
    )
    return image.apt_install("xz-utils").run_commands(f"python3 -c {shlex.quote(command)}")
