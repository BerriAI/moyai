"""Build the real sdist and wheel; source checkouts must not mask missing resources."""

import hashlib
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
    result = subprocess.run(
        [uv, "build", "--out-dir", str(tmp_path)],
        cwd=ROOT / "native", capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel, = tmp_path.glob("*.whl")
    sdist, = tmp_path.glob("*.tar.gz")
    canonical = (ROOT / "sandbox/artifacts.py").read_bytes()
    with tarfile.open(sdist) as source:
        resource, = [entry for entry in source.getmembers() if entry.name.endswith("/_build_resources/legacy_artifacts.py")]
        archived = source.extractfile(resource)
        assert archived is not None
        assert archived.read() == canonical
        assert any(entry.name.endswith("/hatch_build.py") for entry in source.getmembers())
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
        required = {
            "moyai/resources/instructions.md", "moyai/resources/desktop.py",
            "moyai/resources/browser_guest.py", "moyai/resources/legacy_artifacts.py",
            "moyai/resources/skills/coding/SKILL.md", "moyai/static/index.html",
            "moyai/static/moyai.glb", "moyai/input_attachments.py",
            "moyai/product_start.py", "moyai/product_memory_review.py",
        }
        assert required <= names, sorted(required - names)
        assert hashlib.sha256(package.read("moyai/resources/legacy_artifacts.py")).digest() == hashlib.sha256(canonical).digest()
        index = package.read("moyai/static/index.html").decode()
        assets = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)["\']', index)
        assert assets
        assert all("moyai/static" + asset in names for asset in assets)
