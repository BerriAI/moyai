import json
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
        _env_file=None, modal_token_id="test-id", modal_token_secret="test-secret", independent_agent_runtime=False
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


async def test_independent_prebuild_joins_cancelled_build_before_closing_client(monkeypatch):
    import asyncio
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    async def failed(*args):
        await entered.wait()
        raise RuntimeError('runtime build failed')
    class Clients:
        async def get(self, settings):
            return 'client'
        async def close(self):
            assert stopped.is_set()
    monkeypatch.setattr('build_workspace_image.ModalClients', Clients)
    monkeypatch.setattr('build_workspace_image.modal.App.lookup', SimpleNamespace(aio=AsyncMock(return_value='app')))
    monkeypatch.setattr('build_workspace_image.workspace_image', lambda settings: SimpleNamespace(build=SimpleNamespace(aio=slow)))
    monkeypatch.setattr('build_workspace_image.agent_runtime_image', lambda: SimpleNamespace(build=SimpleNamespace(aio=failed)))
    with pytest.raises(RuntimeError, match='runtime build failed'):
        await build_workspace_image(Settings(_env_file=None, modal_token_id='id', modal_token_secret='secret', independent_agent_runtime=True))


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


@pytest.mark.parametrize('independent_runtime', [False, True])
def test_packaged_mcp_initializes_with_the_agent_interpreter(tmp_path, monkeypatch, independent_runtime):
    from sandbox.agent import hermes_config

    source = Path(__file__).resolve().parents[1]
    runtime = tmp_path / 'workspace-runner'
    for package in ('agent', 'sandbox'):
        shutil.copytree(source / package, runtime / package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    bridge = runtime / 'agent/tools/mcp_bridge.py'
    # The base workspace Python has no bridge dependencies; the agent's
    # interpreter does. Reproduce the missing dependency without an install.
    missing = subprocess.run([sys.executable, '-S', str(bridge)], cwd=tmp_path,
                             capture_output=True, text=True, timeout=15)
    assert missing.returncode != 0 and "No module named 'cryptography'" in missing.stderr

    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'packaging-test-capability')
    server = hermes_config({'model': 'test-model', 'independent_runtime': independent_runtime},
                           'http://127.0.0.1:1', tmp_path)['mcp_servers']['workspace']
    assert server['command'] == sys.executable
    # Relocate only the image's absolute package path, keeping its selected
    # interpreter and environment. initialize requires no broker/model call.
    result = subprocess.run([server['command'], str(bridge)], cwd=tmp_path,
        env=server['env'], input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                           'params': {'protocolVersion': '2025-03-26'}}) + '\n',
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['id'] == 1 and response['result']['protocolVersion'] == '2025-03-26'
    assert response['result']['serverInfo']['name'] == 'hermes-workspace'


@pytest.mark.parametrize('name', ['agent.py', '../computer.py', '/tmp/computer.py'])
def test_runtime_command_rejects_unowned_entrypoints(tmp_path, name):
    from app.runtime_files import RUNTIME_COMMAND

    result = subprocess.run(
        [sys.executable, '-I', '-c', RUNTIME_COMMAND, str(tmp_path), name],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert 'Invalid runtime command' in result.stderr
