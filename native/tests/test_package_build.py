"""Build the real sdist and wheel; source checkouts must not mask missing resources."""

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_sdist_to_wheel_retains_runtime_resources_and_canonical_archive_helper(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The uv build CLI is required for package qualification.")
    source_root = tmp_path / "source"
    for directory in ("native", "frontend", "app"):
        shutil.copytree(
            ROOT / directory, source_root / directory,
            ignore=shutil.ignore_patterns(
                ".venv", "node_modules", ".data", ".env", ".env.*", "dist",
                "__pycache__", ".pytest_cache", "test-results", "static",
            ) if directory == "native" else shutil.ignore_patterns("node_modules", "__pycache__"),
        )
    for filename in ("package.json", "package-lock.json", "tsconfig.json"):
        shutil.copy2(ROOT / filename, source_root / filename)
    assert not (source_root / "native/src/moyai/static").exists()
    distribution = tmp_path / "distribution"
    result = subprocess.run(
        [uv, "build", "--out-dir", str(distribution)],
        cwd=source_root / "native", capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = distribution.glob("*.whl")
    sdist, = distribution.glob("*.tar.gz")
    canonical = (ROOT / "app/legacy_artifacts.py").read_bytes()
    with tarfile.open(sdist) as source:
        resource, = [entry for entry in source.getmembers() if entry.name.endswith("/_build_resources/app/legacy_artifacts.py")]
        archived = source.extractfile(resource)
        assert archived is not None
        assert archived.read() == canonical
        assert any(entry.name.endswith("/hatch_build.py") for entry in source.getmembers())
        assert any(entry.name.endswith("/src/moyai/static/index.html") for entry in source.getmembers())
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
        required = {
            "moyai/resources/instructions.md", "moyai/resources/desktop.py",
            "moyai/resources/browser_guest.py", "moyai/resources/browser_driver.py", "app/legacy_artifacts.py",
            "moyai/resources/skills/coding/SKILL.md", "moyai/static/index.html",
            "moyai/static/moyai.glb", "moyai/input_attachments.py",
            "moyai/product_start.py", "moyai/product_memory_review.py",
            "moyai/install_config.py",
        }
        required |= {"app/config.py", "app/schema_initializers.py", "app/schema_versions.json", "app/security.py"}
        assert required <= names, sorted(required - names)
        assert "app/main.py" not in names and not any(name.startswith(("agent/", "sandbox/")) for name in names)
        assert hashlib.sha256(package.read("app/legacy_artifacts.py")).digest() == hashlib.sha256(canonical).digest()
        index = package.read("moyai/static/index.html").decode()
        assets = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)["\']', index)
        assert assets
        assert all("moyai/static" + asset in names for asset in assets)

    commands = tmp_path / "commands"
    commands.mkdir()
    npm = commands / "npm"
    npm.write_text("#!/bin/sh\nexit 91\n")
    npm.chmod(0o755)
    rebuilt = tmp_path / "rebuilt"
    result = subprocess.run(
        [uv, "build", "--wheel", str(sdist), "--out-dir", str(rebuilt)],
        cwd=tmp_path, env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rebuilt_wheel, = rebuilt.glob("*.whl")
    with zipfile.ZipFile(wheel) as original, zipfile.ZipFile(rebuilt_wheel) as reconstructed:
        for name in original.namelist():
            if name.startswith(("moyai/static/", "moyai/resources/")):
                assert reconstructed.read(name) == original.read(name), name
