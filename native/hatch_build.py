"""Build the native UI and retain runtime resources in source and wheel distributions."""

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
