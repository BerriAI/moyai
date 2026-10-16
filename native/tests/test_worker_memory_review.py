"""Background review failures must not consume manual dispatch or cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from temporalio.testing import WorkflowEnvironment

from moyai.product_memory_review import MemoryReviewWorkflow
from moyai.settings_runtime import SETTINGS_WORKER_PREFIX
from moyai.storage import encrypted_converter
from moyai.worker import dispatch_once, make_worker


@pytest.mark.asyncio
async def test_review_registered_with_sdk_without_enabling_public_flag(runtime):
    review = SimpleNamespace(
        agent=Agent("test", name="moyai_memory_review", capabilities=[TemporalDurability()]),
        registered=False,
    )
    runtime.product_memory_review = review
    async with await WorkflowEnvironment.start_local(
        data_converter=encrypted_converter(runtime.security, runtime.objects),
        plugins=[PydanticAIPlugin()],
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime) as worker:
            async with asyncio.timeout(5):
                while not worker.is_running:
                    await asyncio.sleep(0.01)
            assert worker.is_running
            assert worker.config()["identity"].startswith(SETTINGS_WORKER_PREFIX)
            assert worker.config()["identity"].endswith("-memory-review-v1-")
            assert MemoryReviewWorkflow.__pydantic_ai_agents__ == [review.agent]
            assert not review.registered


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("unavailable"), TimeoutError()])
async def test_review_failure_preserves_manual_dispatch(runtime, actor, failure):
    from moyai.contracts import NewMessage, NewSession

    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="manual", request_key="manual-before-review"),
    )
    client = SimpleNamespace(start_workflow=AsyncMock())
    runtime.connect = AsyncMock(return_value=client)

    async def review_dispatch(value, *, limit):
        assert value is runtime and limit == 5
        assert client.start_workflow.await_args.kwargs["id"] == turn.workflow_id
        raise failure

    runtime.product_memory_review = SimpleNamespace(dispatch_pending=review_dispatch)
    assert await dispatch_once(runtime) == 1
    client.start_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_wait_is_bounded_and_dispatcher_cancellation_propagates(runtime, monkeypatch):
    import moyai.worker as module

    client = SimpleNamespace(start_workflow=AsyncMock())
    runtime.connect = AsyncMock(return_value=client)
    entered = asyncio.Event()

    async def review_dispatch(value, *, limit):
        entered.set()
        await asyncio.Event().wait()

    runtime.product_memory_review = SimpleNamespace(dispatch_pending=review_dispatch)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(module.asyncio, "timeout", lambda seconds: real_timeout(0.02))
    assert await dispatch_once(runtime) == 0
    assert entered.is_set()
    entered.clear()
    monkeypatch.setattr(module.asyncio, "timeout", real_timeout)
    task = asyncio.create_task(dispatch_once(runtime))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
