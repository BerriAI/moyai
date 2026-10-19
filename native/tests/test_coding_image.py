"""Exercise the shell boundary used by Modal's generated Dockerfile RUN."""
import subprocess
from typing import cast
from unittest.mock import Mock

import modal
import pytest

from moyai import coding_image


@pytest.mark.parametrize("script,expected", [
    ("set -eu\nvalue='two words'\nprintf '%s\\n' \"$value\"\n", "two words\n"),
    ("set -eu\ncase quoted in\n  quoted) printf '%s' '\"$literal\"' ;;\nesac\n", '"$literal"'),
])
def test_multiline_installer_survives_one_docker_run_instruction(monkeypatch, script, expected):
    image = Mock(spec=modal.Image)
    image.apt_install.return_value = image
    image.run_commands.return_value = image
    monkeypatch.setattr(coding_image, "node_install_script", lambda: script)

    coding_image.with_node(cast(modal.Image, image))
    command = image.run_commands.call_args.args[0]
    assert "\n" not in command, "Modal must not parse shell lines as Dockerfile instructions"
    result = subprocess.run(["sh", "-c", command], check=True, capture_output=True, text=True)
    assert result.stdout == expected


def test_installer_failure_fails_the_image_command(monkeypatch):
    image = Mock(spec=modal.Image)
    image.apt_install.return_value = image
    image.run_commands.return_value = image
    monkeypatch.setattr(coding_image, "node_install_script", lambda: "exit 23\n")

    coding_image.with_node(cast(modal.Image, image))
    result = subprocess.run(
        ["sh", "-c", image.run_commands.call_args.args[0]],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode != 0
