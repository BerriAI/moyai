"""Ship canonical product services and recovery formats in source and wheel builds."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        repository = root.parent
        if (repository / "package-lock.json").is_file():
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError("Building Moyai from the repository requires Node.js 22 and npm.")
            subprocess.run([npm, "ci"], cwd=repository, check=True)
            subprocess.run([npm, "run", "build:native"], cwd=repository, check=True)
        assets = root / "src" / "moyai" / "static"
        if not (assets / "index.html").is_file():
            raise FileNotFoundError(
                "The native UI is missing. Build from the repository with Node.js and npm, "
                "or use a Moyai source distribution containing the compiled UI."
            )
        asset_destination = "moyai/static" if self.target_name == "wheel" else "src/moyai/static"
        build_data["force_include"][str(assets)] = asset_destination
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
