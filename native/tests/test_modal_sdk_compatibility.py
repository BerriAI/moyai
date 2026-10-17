"""Exercise the pinned Harness/Modal error boundary without a cloud sandbox."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import modal
import pytest
from pydantic_ai.workspaces import WorkspaceUnavailableError
from pydantic_ai_harness.modal_sandbox import ModalSandboxBackend


@pytest.mark.parametrize("failure", [
    ConnectionError("Synthetic sandbox hostname resolution failed"),
    TimeoutError("Synthetic command-router connection timed out"),
    modal.exception.ConnectionError("Synthetic Modal transport connection failed"),
])
async def test_workspace_startup_preserves_connection_failure(failure):
    execute = AsyncMock(side_effect=failure)
    sandbox = SimpleNamespace(
        object_id="sb-synthetic-compatibility-check", exec=SimpleNamespace(aio=execute),
    )
    backend = ModalSandboxBackend(sandbox=sandbox, working_dir="/workspace")

    with pytest.raises(type(failure)) as caught:
        await backend.working_dir()

    assert caught.value is failure
    # A failed initialization probe must not reach or repeat the requested command.
    assert execute.await_count == 1


async def test_workspace_startup_keeps_rejected_credentials_explicit():
    execute = AsyncMock(side_effect=modal.exception.AuthError("Synthetic rejected credentials"))
    sandbox = SimpleNamespace(
        object_id="sb-synthetic-compatibility-check", exec=SimpleNamespace(aio=execute),
    )
    backend = ModalSandboxBackend(sandbox=sandbox, working_dir="/workspace")

    with pytest.raises(WorkspaceUnavailableError, match="Modal rejected the credentials"):
        await backend.working_dir()

    assert execute.await_count == 1
