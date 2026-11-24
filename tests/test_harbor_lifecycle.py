"""The explicit evaluation boundary never authenticates to a real Modal account."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_harbor_lifecycle_probes_real_container_and_refuses_snapshots(monkeypatch):
    pytest.importorskip("harbor")
    import modal

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "evals"))
    from moyai_harbor.lifecycle import harbor_lifecycle

    environment = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=0)))
    workspace = SimpleNamespace(environment=environment, reference=SimpleNamespace(id="harbor-eval-owned"))
    original = modal.Sandbox.from_id.aio
    with harbor_lifecycle(workspace):
        sandbox = await modal.Sandbox.from_id.aio("harbor-eval-owned")
        assert await sandbox.poll.aio() is None
        process = await sandbox.exec.aio("/bin/true", timeout=10)
        assert await process.wait.aio() == 0
        assert environment.exec.await_count == 2
        environment.exec.assert_awaited_with("/bin/true", cwd="/workspace", timeout_sec=10)
        with pytest.raises(NotImplementedError, match="do not provide Modal"):
            await sandbox.snapshot_filesystem.aio(timeout=120)
        with pytest.raises(RuntimeError, match="different Modal"):
            await modal.Sandbox.from_id.aio("unrelated-production-reference")
        with pytest.raises(RuntimeError, match="read-only"):
            await sandbox.exec.aio("rm", "-rf", "/workspace")
        environment.exec.return_value.return_code = 1
        assert await sandbox.poll.aio() == 1
    assert modal.Sandbox.from_id.aio == original
