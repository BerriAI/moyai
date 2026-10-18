from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import modal

from moyai.tools import WORKSPACE_TOOLS, OpenComputerUse


class FakeImage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def apt_install(self, *packages: str) -> FakeImage:
        self.calls.append(("apt_install", packages))
        return self

    def add_local_file(
        self, local_path: str | Path, remote_path: str, *, copy: bool = False,
    ) -> FakeImage:
        self.calls.append(("add_local_file", (str(local_path), remote_path, str(copy))))
        return self

    def run_commands(self, *commands: str) -> FakeImage:
        self.calls.append(("run_commands", commands))
        return self


def test_open_computer_use_installs_accessibility_dependencies() -> None:
    image = FakeImage()

    assert OpenComputerUse().install(cast(modal.Image, image)) is image
    assert image.calls[0] == (
        "apt_install", ("dbus-x11", "at-spi2-core", "python3-gi", "gir1.2-atspi-2.0"),
    )
    tools_directory = Path(__file__).resolve().parents[1] / "src" / "moyai" / "tools"
    assert image.calls[1] == (
        "add_local_file", (str(tools_directory / "package.json"), "/opt/moyai/tools/package.json", "True"),
    )
    assert image.calls[2] == (
        "add_local_file",
        (str(tools_directory / "package-lock.json"), "/opt/moyai/tools/package-lock.json", "True"),
    )
    assert image.calls[3][0] == "run_commands"
    assert len(image.calls[3][1]) == 1
    command = image.calls[3][1][0]
    assert "npm ci --ignore-scripts --prefix /opt/moyai/tools" in command
    assert "ln -sf /opt/moyai/tools/node_modules/.bin/ocu /usr/local/bin/ocu" in command
    assert "@" not in command
    assert "http" not in command


def test_open_computer_use_is_a_workspace_tool() -> None:
    assert any(isinstance(tool, OpenComputerUse) for tool in WORKSPACE_TOOLS)


def test_open_computer_use_package_lock_is_pinned() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "src" / "moyai" / "tools" / "package-lock.json"
    lock = json.loads(lock_path.read_text())
    package = lock["packages"]["node_modules/open-computer-use"]

    assert package["version"] == "1.1.2"
    assert package["integrity"] == (
        "sha512-xb88Gwf3y0NgRY2qVkC+Ff00lSBOR5tD7vQCIph+YoYaPrZ6FPata7UW9fR5zowGrHF3aK/E0buutXl0TlUvnQ=="
    )
    assert package["resolved"] == (
        "https://registry.npmjs.org/open-computer-use/-/open-computer-use-1.1.2.tgz"
    )
