from __future__ import annotations

import modal

from .base import WorkspaceTool


class OpenComputerUse(WorkspaceTool):
    VERSION = "1.1.2"
    APT_PACKAGES = ("dbus-x11", "at-spi2-core", "python3-gi", "gir1.2-atspi-2.0")

    def install(self, image: modal.Image) -> modal.Image:
        return image.apt_install(*self.APT_PACKAGES).run_commands(
            f"npm i -g open-computer-use@{self.VERSION}",
        )
