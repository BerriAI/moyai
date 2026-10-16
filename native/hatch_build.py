"""Carry the canonical archive helper through both source and wheel builds."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        # A wheel built from the published sdist cannot reach ../sandbox.
        # The sdist carries the exact canonical bytes captured at its build.
        source = root / "_build_resources" / "legacy_artifacts.py"
        if not source.is_file():
            source = root.parent / "sandbox" / "artifacts.py"
        if not source.is_file():
            raise FileNotFoundError("The canonical legacy archive helper is missing from the build inputs.")
        destination = (
            "moyai/resources/legacy_artifacts.py"
            if self.target_name == "wheel" else "_build_resources/legacy_artifacts.py"
        )
        build_data["force_include"][str(source)] = destination
