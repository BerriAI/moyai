"""Public projections of native SDK events; private reasoning is never returned."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent

from .commentary import text_phase
from .live_events import ResponseStart

PRIVATE_TOOLS = {"read_memory", "search_memory", "write_memory", "delete_memory", "load_capability"}


def private_tool(name: str | None) -> bool:
    return name in PRIVATE_TOOLS or bool(name and name.startswith("credentials_"))


def public_event(event: Any) -> dict[str, Any] | None:
    if isinstance(event, ResponseStart):
        return {"type": "response_start", "run_id": event.run_id, "step": event.step}
    if isinstance(event, PartStartEvent):
        if event.part.part_kind == "text":
            return {
                "type": "text_start", "index": event.index, "text": event.part.content,
                "phase": text_phase(event.part.provider_details),
            }
        return None
    if isinstance(event, PartDeltaEvent):
        if event.delta.part_delta_kind == "text":
            return {
                "type": "text_delta", "index": event.index, "text": event.delta.content_delta,
                "phase": text_phase(event.delta.provider_details),
            }
        return None
    if isinstance(event, PartEndEvent):
        if event.part.part_kind == "text":
            return {
                "type": "text_end", "index": event.index, "text": event.part.content,
                "phase": text_phase(event.part.provider_details),
            }
        return None
    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        return {
            "type": "tool_call", "id": part.tool_call_id, "name": part.tool_name,
            "arguments": None if private_tool(part.tool_name) else part.args,
        }
    if isinstance(event, FunctionToolResultEvent):
        part = event.part
        content = str(getattr(part, "content", ""))
        return {
            "type": "tool_result", "id": part.tool_call_id,
            "name": part.tool_name,
            "content": None if private_tool(part.tool_name) else content[:30_000],
            "failed": part.part_kind == "retry-prompt" or getattr(part, "outcome", None) == "failed",
        }
    if isinstance(event, AgentRunResultEvent):
        return {"type": "agent_finished", "run_id": event.result.run_id}
    name = type(event).__name__
    if (
        name.startswith(("Plan", "Delegation", "AskUser", "EnqueuedMessages"))
        and is_dataclass(event) and not isinstance(event, type)
    ):
        value = asdict(event)
        # Enqueued messages may include model-native metadata; public inputs already have app records.
        if name.startswith("EnqueuedMessages"):
            value = {"enqueue_id": value.get("enqueue_id")}
        return {"type": "capability", "name": name, "data": value}
    return None


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) > 5:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    return value


def saved_activity(messages: list[ModelMessage], run_id: str) -> list[dict[str, Any]]:
    from .history_handoff import is_turn_run

    result = []
    for message in messages:
        if not is_turn_run(message.run_id, run_id):
            continue
        for part in message.parts:
            if isinstance(message, ModelResponse) and isinstance(part, ToolCallPart):
                item = public_event(FunctionToolCallEvent(part))
            elif isinstance(message, ModelRequest) and isinstance(part, ToolReturnPart):
                item = public_event(FunctionToolResultEvent(part))
            else:
                continue
            if item is not None:
                result.append(item)
    return result

