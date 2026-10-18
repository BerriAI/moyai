from __future__ import annotations

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

    assert OpenComputerUse().install(image) is image
    assert image.calls == [
        ("apt_install", ("dbus-x11", "at-spi2-core", "python3-gi", "gir1.2-atspi-2.0")),
        ("run_commands", ("npm i -g open-computer-use@1.1.2",)),
    ]


def test_open_computer_use_is_a_workspace_tool() -> None:
    assert any(isinstance(tool, OpenComputerUse) for tool in WORKSPACE_TOOLS)
