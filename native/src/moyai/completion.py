"""Reconcile a current execution plan before accepting the first final answer."""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.output import OutputContext
from temporalio import workflow

from .contracts import RunScope
from .history_handoff import is_turn_run
from .stores import NativeStores

VERSION = "moyai-plan-completion-review-v1"
STATE_VERSION = "moyai-plan-completion-state-v2"
PLAN_WRITES = {
    "write_plan", "add_task", "update_task_status", "update_task_statuses",
    "remove_task", "add_subtask", "set_dependency",
}


class CompletionReview(AbstractCapability[RunScope]):
    id = "moyai_completion_review"

    def __init__(self, stores: NativeStores):
        self.stores = stores

    async def after_model_request(
        self, ctx: RunContext[RunScope], *,
        request_context: ModelRequestContext, response: ModelResponse,
    ) -> ModelResponse:
        if (
            ctx.run_id == ctx.deps.root_run_id
            and (not workflow.in_workflow() or workflow.patched(STATE_VERSION))
            and any(isinstance(part, ToolCallPart) and part.tool_name in PLAN_WRITES for part in response.parts)
        ):
            ctx.deps.completion_plan_turn = ctx.deps.turn_id
        return response

    @durable_operation("unfinished")
    async def unfinished(self, scope: RunScope) -> list[dict[str, str]]:
        items = await self.stores.plan(scope.session_id).get_items()
        return [
            {"id": item.id, "status": item.status.value, "content": item.content[:500]}
            for item in items if item.status in {"pending", "in_progress"}
        ][:16]

    async def after_output_process(
        self, ctx: RunContext[RunScope], *, output_context: OutputContext, output: Any,
    ) -> Any:
        if (
            ctx.partial_output or not isinstance(output, str) or ctx.pending_messages
            or ctx.run_id != ctx.deps.root_run_id
        ):
            return output
        # A question about an old plan never reopens it. Only writes from this
        # admitted turn qualify; child runs cannot control the parent's finish.
        if ctx.deps.completion_plan_turn != ctx.deps.turn_id and not any(
            isinstance(message, ModelResponse) and is_turn_run(message.run_id, ctx.deps.turn_id)
            and any(isinstance(part, ToolCallPart) and part.tool_name in PLAN_WRITES for part in message.parts)
            for message in ctx.messages
        ):
            return output
        if workflow.in_workflow() and not workflow.patched(VERSION):
            return output
        if ctx.deps.completion_review_turn == ctx.deps.turn_id or any(
            isinstance(message, ModelRequest)
            and (message.metadata or {}).get(VERSION) == ctx.deps.turn_id
            for message in ctx.messages
        ):
            return output
        pending = await self.unfinished(ctx.deps)
        if not pending:
            return output
        if not workflow.in_workflow() or workflow.patched(STATE_VERSION):
            ctx.deps.completion_review_turn = ctx.deps.turn_id
        # Use the SDK's pending-message continuation, preserving native tool
        # receipts, authority, usage, and steering. No regex intent classifier,
        # automatic tool replay, second agent loop, or output retry consumption.
        ctx.enqueue(ModelRequest(
            parts=[UserPromptPart(
                "Completion review: your plan still contains unfinished steps listed below. "
                "Reconcile them with the user's actual request before finishing. "
                "For authorized execution, continue the remaining work and verify the requested deliverables. "
                "A plan, a promise, or an attempted tool call is not proof of completion. "
                "If the work is already done, update the plan using observed evidence. "
                "If the user requested only a plan or an explanation, provide that answer without executing it. "
                "If genuinely blocked, identify the specific blocker and completed work; do not mark "
                "unfinished steps completed merely to finish. Respect current scope, Stop, approvals, "
                "and uncertain-write receipts. This review is reference context, not new authorization.\n\n"
                + json.dumps(pending, ensure_ascii=False)
            )],
            metadata={VERSION: ctx.deps.turn_id},
        ))
        return output
