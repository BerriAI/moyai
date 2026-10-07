from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import venv

import modal
import pytest

from app.config import Settings
from build_workspace_image import build_workspace_image


def test_harness_installer_bootstraps_a_real_pip_free_environment(tmp_path: Path) -> None:
    venv.EnvBuilder(with_pip=False).create(tmp_path / 'runtime')
    python = str(tmp_path / 'runtime' / 'bin' / 'python')
    missing = subprocess.run([python, '-m', 'pip', '--version'], capture_output=True, text=True)
    assert missing.returncode != 0 and 'No module named pip' in missing.stderr
    subprocess.run([python, '-c', 'from sandbox.harness_dependencies import ensure_pip; ensure_pip()'],
                   check=True, capture_output=True, text=True, timeout=60)
    # Once bootstrapped, an existing environment must need no further install.
    subprocess.run([python, '-c', "from unittest.mock import patch; "
                    "from sandbox.harness_dependencies import ensure_pip; "
                    "\nwith patch('subprocess.run', side_effect=AssertionError('unexpected install')): ensure_pip()"],
                   check=True, capture_output=True, text=True, timeout=10)
    installed = subprocess.run([python, '-m', 'pip', '--version'], check=True, capture_output=True, text=True)
    assert str(tmp_path / 'runtime') in installed.stdout


@pytest.mark.parametrize('fails', [False, True])
async def test_deploy_prebuild_waits_for_the_same_workspace_image(monkeypatch, fails: bool) -> None:
    settings = Settings(_env_file=None, modal_token_id='test-id', modal_token_secret='test-secret')
    client = AsyncMock(return_value='client')
    app = AsyncMock(return_value='app')
    build = AsyncMock(side_effect=modal.exception.ImageBuildError('failed', 'im-test') if fails else None)
    image = SimpleNamespace(build=SimpleNamespace(aio=build))
    monkeypatch.setattr('build_workspace_image.modal.Client.from_credentials', SimpleNamespace(aio=client))
    monkeypatch.setattr('build_workspace_image.modal.App.lookup', SimpleNamespace(aio=app))
    monkeypatch.setattr('build_workspace_image.workspace_image', lambda current: image if current is settings else None)
    if fails:
        with pytest.raises(modal.exception.ImageBuildError):
            await build_workspace_image(settings)
    else:
        assert await build_workspace_image(settings) is image
    build.assert_awaited_once_with('app')
    app.assert_awaited_once_with(settings.modal_app_name, create_if_missing=True, client='client')


async def test_prebuild_requires_complete_credentials() -> None:
    with pytest.raises(ValueError, match='MODAL_TOKEN'):
        await build_workspace_image(Settings(_env_file=None, modal_token_id='', modal_token_secret=''))
