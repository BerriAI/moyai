"""A reproducible Node runtime shared by fresh and restored coding computers."""

from __future__ import annotations

import modal

# Official https://nodejs.org/dist/v22.23.3/SHASUMS256.txt.
# Debian's Node 18 cannot run the repository's current Vite toolchain.
NODE_VERSION = "22.23.3"
NODE_SHA256 = {
    "amd64": "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de",
    "arm64": "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f",
}


def with_node(image: modal.Image) -> modal.Image:
    return image.apt_install("xz-utils").run_commands(f"""set -eu
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
""")
