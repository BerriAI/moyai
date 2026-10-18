from __future__ import annotations

import modal

from .base import WorkspaceTool


class OpenComputerUse(WorkspaceTool):
    VERSION = "1.1.2"
    APT_PACKAGES = ("dbus-x11", "at-spi2-core", "python3-gi", "gir1.2-atspi-2.0")
    TARBALL_URL = "https://registry.npmjs.org/open-computer-use/-/open-computer-use-1.1.2.tgz"
    TARBALL_SHA256 = "393d83a04c7f34edf4cb1d5b00ffee400fa20bf6725457493cda479420c5703e"

    def install(self, image: modal.Image) -> modal.Image:
        command = (
            f"curl -fsSL -o /tmp/ocu.tgz {self.TARBALL_URL} && "
            f'echo "{self.TARBALL_SHA256}  /tmp/ocu.tgz" | sha256sum -c - && '
            "npm install -g --ignore-scripts /tmp/ocu.tgz && "
            "rm /tmp/ocu.tgz"
        )
        return image.apt_install(*self.APT_PACKAGES).run_commands(command)
