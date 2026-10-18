from __future__ import annotations

from pathlib import Path

import modal

from .base import WorkspaceTool


class OpenComputerUse(WorkspaceTool):
    APT_PACKAGES = ("dbus-x11", "at-spi2-core", "python3-gi", "gir1.2-atspi-2.0")

    def install(self, image: modal.Image) -> modal.Image:
        tools_directory = Path(__file__).parent
        return (
            image.apt_install(*self.APT_PACKAGES)
            .add_local_file(tools_directory / "package.json", "/opt/moyai/tools/package.json", copy=True)
            .add_local_file(tools_directory / "package-lock.json", "/opt/moyai/tools/package-lock.json", copy=True)
            .run_commands(
                "npm ci --ignore-scripts --prefix /opt/moyai/tools && "
                "ln -sf /opt/moyai/tools/node_modules/.bin/ocu /usr/local/bin/ocu",
            )
            .env({"DISPLAY": ":99", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/moyai-dbus",
                  "XDG_RUNTIME_DIR": "/tmp/moyai-xdg", "NO_AT_BRIDGE": "0", "GTK_A11Y": "always"})
        )
