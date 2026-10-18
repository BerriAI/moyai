from __future__ import annotations

from typing import cast

import modal

from moyai.tools import WORKSPACE_TOOLS, OpenComputerUse


class FakeImage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def apt_install(self, *packages: str) -> FakeImage:
        self.calls.append(("apt_install", packages))
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
    assert image.calls[1][0] == "run_commands"
    assert len(image.calls[1][1]) == 1
    command = image.calls[1][1][0]
    assert OpenComputerUse.TARBALL_URL in command
    assert OpenComputerUse.TARBALL_SHA256 in command
    assert "sha256sum -c -" in command
    assert "--ignore-scripts" in command
    assert "open-computer-use@" not in command


def test_open_computer_use_is_a_workspace_tool() -> None:
    assert any(isinstance(tool, OpenComputerUse) for tool in WORKSPACE_TOOLS)
