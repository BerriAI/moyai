"""Explicit lifecycle fixture for Harbor's synthetic Modal-compatible workspace.

Agent commands still execute in the real Harbor container. These trials do not
qualify Modal snapshots or recovery; actual-app E2E covers those separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import modal

from .workspace import HarborWorkspace


@contextmanager
def harbor_lifecycle(workspace: HarborWorkspace) -> Iterator[None]:
    async def poll() -> int | None:
        result = await workspace.environment.exec("/bin/true", cwd="/workspace", timeout_sec=10)
        return None if result.return_code == 0 else result.return_code

    async def execute(*command: str, timeout: float = 10):
        if command != ("/bin/true",):
            raise RuntimeError("The Harbor lifecycle fixture only permits the read-only keepalive probe.")
        result = await workspace.environment.exec("/bin/true", cwd="/workspace", timeout_sec=int(timeout))

        async def wait() -> int:
            return result.return_code

        return SimpleNamespace(wait=SimpleNamespace(aio=wait))

    async def snapshot_filesystem(*, timeout: float):
        raise NotImplementedError("Harbor trials do not provide Modal filesystem checkpoints.")

    sandbox = SimpleNamespace(
        poll=SimpleNamespace(aio=poll),
        exec=SimpleNamespace(aio=execute),
        snapshot_filesystem=SimpleNamespace(aio=snapshot_filesystem),
    )

    async def lookup(identity: str):
        if identity != workspace.reference.id:
            raise RuntimeError("A Harbor trial attempted to access a different Modal workspace.")
        return sandbox

    # The CI command deliberately runs one trial at a time per process (-n 1).
    # Unknown references fail closed instead of reaching a real Modal account.
    with patch.object(modal.Sandbox.from_id, "aio", lookup):
        yield
