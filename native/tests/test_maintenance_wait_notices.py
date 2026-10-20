"""A UI notice cannot change a maintenance safety decision."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from moyai import maintenance_workspace as module
from moyai.contracts import PreparedTurn, RunScope
from moyai.history_handoff import AgentCheckpoint
from moyai.product_continuation_errors import preparation_blocked


def graph():
    return AgentCheckpoint(
        prepared=PreparedTurn(RunScope("session", "turn", "actor", "test"), ""),
        conversation="{}", workspace=None, run_step=0, output_retries_used=0,
    )


@asynccontextmanager
async def unavailable():
    raise ConnectionError("The notice database is unavailable.")
    yield  # pragma: no cover


def runtime(monkeypatch, begin=unavailable):
    value = SimpleNamespace(database=SimpleNamespace(sessions=SimpleNamespace(begin=begin)))
    monkeypatch.setattr("moyai.runtime.current", lambda: value)
    monkeypatch.setattr(module.activity, "info", lambda: SimpleNamespace(workflow_id="workflow", workflow_run_id="run"))
    return value


@pytest.mark.parametrize("code", ["workspace_in_use", "workspace_uncertain_effect", "capture_unknown"])
async def test_failed_notice_does_not_mask_classified_capture_refusal(monkeypatch, code):
    runtime(monkeypatch)
    original = module.uncertain_capture() if code == "capture_unknown" else preparation_blocked(code)
    monkeypatch.setattr(module, "preserve", AsyncMock(side_effect=original))
    with pytest.raises(ApplicationError) as result:
        await module.save_maintenance_checkpoint(graph(), 22, "native_node:2")
    assert result.value is original
    assert module.wait_reason(result.value) == code


async def test_failed_notice_does_not_mask_classified_restore_wait(monkeypatch):
    runtime(monkeypatch)
    monkeypatch.setattr(module, "resume", AsyncMock(side_effect=ConnectionError("The provider reply was lost.")))
    with pytest.raises(ApplicationError) as result:
        await module.load_maintenance_checkpoint("turn", "reference", "previous")
    assert result.value.type == "MoyaiMaintenanceRestorePending"


async def test_notice_timeout_is_bounded_and_preserves_capture_refusal(monkeypatch):
    @asynccontextmanager
    async def blocked():
        await asyncio.Event().wait()
        yield

    runtime(monkeypatch, blocked)
    monkeypatch.setattr(module, "WAIT_NOTICE_SECONDS", .01)
    original = preparation_blocked("workspace_in_use")
    monkeypatch.setattr(module, "preserve", AsyncMock(side_effect=original))
    with pytest.raises(ApplicationError) as result:
        await asyncio.wait_for(module.save_maintenance_checkpoint(graph(), 22, "native_node:2"), 1)
    assert result.value is original


async def test_notice_does_not_swallow_cancellation(monkeypatch):
    entered = asyncio.Event()

    @asynccontextmanager
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
        yield

    value = runtime(monkeypatch, blocked)
    pending = asyncio.create_task(module.publish_wait(value, "turn", "capture_unknown"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
