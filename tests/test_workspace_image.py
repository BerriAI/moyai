import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import modal
import pytest

from app.config import Settings
from build_workspace_image import build_workspace_image


def test_harness_installer_bootstraps_a_real_pip_free_environment(
    tmp_path: Path,
) -> None:
    venv.EnvBuilder(with_pip=False).create(tmp_path / "runtime")
    python = str(tmp_path / "runtime" / "bin" / "python")
    missing = subprocess.run(
        [python, "-m", "pip", "--version"], check=False, capture_output=True, text=True
    )
    assert missing.returncode != 0 and "No module named pip" in missing.stderr
    subprocess.run(
        [
            python,
            "-c",
            "from sandbox.harness_dependencies import ensure_pip; ensure_pip()",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Once bootstrapped, an existing environment must need no further install.
    subprocess.run(
        [
            python,
            "-c",
            (
                "from unittest.mock import patch; "
                "from sandbox.harness_dependencies import ensure_pip; "
                "\nwith patch('subprocess.run', side_effect=AssertionError('unexpected install')): ensure_pip()"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    installed = subprocess.run(
        [python, "-m", "pip", "--version"], check=True, capture_output=True, text=True
    )
    assert str(tmp_path / "runtime") in installed.stdout


@pytest.mark.parametrize("failure", [None, "lookup", "build"])
async def test_deploy_prebuild_waits_for_the_same_workspace_image_and_closes_client(
    monkeypatch, failure: str | None
) -> None:
    settings = Settings(
        _env_file=None, modal_token_id="test-id", modal_token_secret="test-secret"
    )
    connection = SimpleNamespace(__aexit__=AsyncMock())
    client = AsyncMock(return_value=connection)
    error = modal.exception.ImageBuildError("failed", "im-test")
    app = AsyncMock(
        return_value="app", side_effect=error if failure == "lookup" else None
    )
    build = AsyncMock(side_effect=error if failure == "build" else None)
    image = SimpleNamespace(build=SimpleNamespace(aio=build))
    monkeypatch.setattr(
        "build_workspace_image.modal.Client.from_credentials",
        SimpleNamespace(aio=client),
    )
    monkeypatch.setattr(
        "build_workspace_image.modal.App.lookup", SimpleNamespace(aio=app)
    )
    monkeypatch.setattr(
        "build_workspace_image.workspace_image",
        lambda current: image if current is settings else None,
    )
    if failure:
        with pytest.raises(modal.exception.ImageBuildError) as raised:
            await build_workspace_image(settings)
        assert raised.value is error
    else:
        assert await build_workspace_image(settings) is image
    if failure == "lookup":
        build.assert_not_awaited()
    else:
        build.assert_awaited_once_with("app")
    client.assert_awaited_once_with("test-id", "test-secret")
    app.assert_awaited_once_with(
        settings.modal_app_name, create_if_missing=True, client=connection
    )
    connection.__aexit__.assert_awaited_once_with(None, None, None)


async def test_prebuild_requires_complete_credentials() -> None:
    with pytest.raises(ValueError, match="MODAL_TOKEN"):
        await build_workspace_image(
            Settings(_env_file=None, modal_token_id="", modal_token_secret="")
        )


@pytest.mark.parametrize('entrypoint', ['sandbox/agent.py', 'agent/tools/mcp_bridge.py'])
def test_packaged_entrypoints_bootstrap_without_controller_or_working_directory(tmp_path, entrypoint):
    source = Path(__file__).resolve().parents[1]
    runtime = tmp_path / 'workspace-runner'
    for package in ('agent', 'sandbox'):
        shutil.copytree(source / package, runtime / package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Old checkpoints still contain this file; the package must take precedence.
    (runtime / 'agent.py').write_text('raise RuntimeError("stale entrypoint imported")')
    result = subprocess.run(
        [sys.executable, '-I', '-c', 'import runpy, sys; runpy.run_path(sys.argv[1])',
         str(runtime / entrypoint)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('name', ['agent.py', '../computer.py', '/tmp/computer.py'])
def test_runtime_command_rejects_unowned_entrypoints(tmp_path, name):
    from app.runtime_files import RUNTIME_COMMAND

    result = subprocess.run(
        [sys.executable, '-I', '-c', RUNTIME_COMMAND, str(tmp_path), name],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert 'Invalid runtime command' in result.stderr
