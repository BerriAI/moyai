"""Ship canonical product services and recovery formats in source and wheel builds."""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        # Both product integration and local development ship in one installation.
        shared = root / "_build_resources" / "app"
        if not shared.is_dir():
            shared = root.parent / "app"
        if not (shared / "config.py").is_file():
            raise FileNotFoundError("The canonical product package is missing from the build inputs.")
        prefix = "app" if self.target_name == "wheel" else "_build_resources/app"
        for path in sorted(shared.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts:
                build_data["force_include"][str(path)] = prefix + "/" + path.relative_to(shared).as_posix()
