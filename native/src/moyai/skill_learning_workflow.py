"""Durable, tool-free review; never delays or changes the completed answer."""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
from pydantic_ai.usage import UsageLimits
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from .product_memory_review import CONTROL, ReviewDeferred, ReviewInput
from .product_skill_learning import SkillLearningService
from .skill_learning_prompt import SkillReview


def current_learning() -> SkillLearningService:
    from .runtime import current
    service = current().product_skill_learning
    if service is None:
        raise TypeError("Skill learning is not configured.")
    return service


@activity.defn(name="moyai_skill_learning_prepare_v1")
async def prepare_learning(turn_id: str) -> ReviewInput:
    return await current_learning().prepare(turn_id)


@activity.defn(name="moyai_skill_learning_apply_v1")
async def apply_learning(prepared: ReviewInput, review: SkillReview) -> int:
    try:
        return await current_learning().apply(prepared, review)
    except ReviewDeferred:
        raise ApplicationError(
            "Skill learning is waiting for foreground work or maintenance.",
            type="MoyaiSkillLearningDeferred", next_retry_delay=timedelta(seconds=30),
        ) from None
    except (HTTPException, PermissionError, ValueError):
        raise ApplicationError(
            "Skill learning was superseded or its evidence was invalid.",
            type="MoyaiSkillLearningSuperseded", non_retryable=True,
        ) from None


@activity.defn(name="moyai_skill_learning_failed_v1")
async def failed_learning(turn_id: str) -> None:
    await current_learning().failed(turn_id)


@workflow.defn(name="MoyaiSkillLearningV1", sandboxed=False)
class SkillLearningWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: Sequence[AbstractAgent[Any, Any]] = ()

    @workflow.run
    async def run(self, turn_id: str) -> dict[str, Any]:
        try:
            while True:
                prepared = await workflow.execute_activity(
                    "moyai_skill_learning_prepare_v1", turn_id, result_type=ReviewInput, **CONTROL,
                )
                if not prepared.wait_seconds:
                    break
                await workflow.sleep(prepared.wait_seconds)
            if prepared.scope is None:
                return {"status": "skipped", "saved_count": 0}
            service = current_learning()
            result = await service.agent.run(
                (prepared.prompts or [""])[0], deps=prepared.scope, model=service.models[prepared.scope.model],
                run_id=f"skill-learning:{turn_id}", usage_limits=UsageLimits(request_limit=1),
            )
            count = await workflow.execute_activity(
                apply_learning, args=[prepared, result.output],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
            return {"status": "completed", "saved_count": count}
        except Exception:  # noqa: BLE001 - no private errors enter the public conversation
            await workflow.execute_activity(failed_learning, turn_id, **CONTROL)
            return {"status": "failed", "saved_count": 0}


SKILL_LEARNING_WORKFLOWS = [SkillLearningWorkflow]
SKILL_LEARNING_ACTIVITIES = [prepare_learning, apply_learning, failed_learning]
