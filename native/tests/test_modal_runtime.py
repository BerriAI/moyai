"""Deployment configuration tests; actual VM/Chromium proof runs separately."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from modal.config import config
from pydantic import ValidationError

from moyai.modal_runtime import configure_modal_runtime
from moyai.runtime import Runtime
from moyai.settings import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Runtime's SDK configuration is deliberately process-wide. Preserve the
    # caller's environment when testing different deployments in one process.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("MOYAI_MODAL_RUNTIME", raising=False)
    monkeypatch.delenv("MODAL_FUNCTION_RUNTIME", raising=False)


@pytest.mark.parametrize("name", ["MOYAI_MODAL_RUNTIME", "MODAL_FUNCTION_RUNTIME"])
@pytest.mark.parametrize("selected", ["vm", "gvisor"])
async def test_application_initializes_sdk_before_agent_or_recovery(tmp_path, monkeypatch, name, selected):
    monkeypatch.setenv(name, selected)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    runtime = Runtime(settings)
    try:
        assert settings.modal_runtime == selected
        assert config.get("function_runtime") == selected
        # Identical configuration in another Runtime is harmless.
        configure_modal_runtime(settings)
    finally:
        await runtime.close()


@pytest.mark.parametrize("selected", ["vm", "gvisor"])
def test_dotenv_runtime_is_exported_to_sdk(tmp_path, selected):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MOYAI_MODAL_RUNTIME={selected}\n")
    settings = Settings(_env_file=dotenv)
    configure_modal_runtime(settings)
    assert config.get("function_runtime") == selected


def test_default_uses_vm():
    settings = Settings(_env_file=None)
    configure_modal_runtime(settings)
    assert config.get("function_runtime") == "vm"


@pytest.mark.parametrize("value", ["", "VM", "runc", "invalid"])
def test_invalid_runtime_fails_before_allocation(monkeypatch, value):
    monkeypatch.setenv("MOYAI_MODAL_RUNTIME", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_conflicting_environment_does_not_silently_override_sdk(monkeypatch):
    monkeypatch.setenv("MODAL_FUNCTION_RUNTIME", "gvisor")
    monkeypatch.setenv("MOYAI_MODAL_RUNTIME", "vm")
    with pytest.raises(ValueError, match="conflicts"):
        configure_modal_runtime(Settings(_env_file=None))
    assert config.get("function_runtime") == "gvisor"


def test_different_runtimes_cannot_race_in_one_process():
    configure_modal_runtime(Settings(_env_file=None, modal_runtime="vm"))
    with pytest.raises(ValueError, match="one Modal runtime per process"):
        configure_modal_runtime(Settings(_env_file=None, modal_runtime="gvisor"))
    assert config.get("function_runtime") == "vm"


@pytest.mark.parametrize("selected", [None, "gvisor"])
async def test_image_prewarming_uses_selected_runtime(monkeypatch, selected):
    import build_workspace_image

    async def lookup(*args, **kwargs):
        assert config.get("function_runtime") == (selected or "vm")
        return object()

    image = SimpleNamespace(build=SimpleNamespace(aio=AsyncMock()))
    if selected is not None:
        monkeypatch.setenv("MOYAI_MODAL_RUNTIME", selected)
    monkeypatch.setattr(build_workspace_image.modal.App.lookup, "aio", lookup)
    monkeypatch.setattr(build_workspace_image, "workspace_image", lambda: image)
    assert await build_workspace_image.build_workspace_image(
        Settings(_env_file=None),
    ) is image
    image.build.aio.assert_awaited_once()
