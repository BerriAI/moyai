import zipfile
from pathlib import Path

from scripts.lambda_image import package

from moyai.coding_image import node_install_script


def test_native_image_package_contains_exact_current_browser_and_guest_sources(tmp_path):
    destination = tmp_path / "image.zip"
    package(destination)
    root = Path(__file__).parents[2]
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("install-node.sh").decode() == node_install_script()
        for name in ("desktop.py", "browser_guest.py", "browser_driver.py", "ocu_guest.py"):
            assert archive.read(name) == (root / "native" / name).read_bytes()
        for name in ("package.json", "package-lock.json"):
            assert archive.read("tools/" + name) == (root / "native/src/moyai/tools" / name).read_bytes()
        for name in ("checkpoint.py", "service.py", "Dockerfile"):
            assert archive.read(name) == (root / "native/lambda_guest" / name).read_bytes()
        assert not any(name.startswith(("agent/", "sandbox/")) for name in archive.namelist())
        assert "browser_driver.py" in archive.read("Dockerfile").decode()
