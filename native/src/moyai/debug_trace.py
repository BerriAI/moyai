"""Requester-only export of private reasoning and tool details."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import select

from .history_handoff import is_turn_run, latest_turn_snapshot
from .models import Message, Turn, User
from .presentation import private_tool
from .runtime import Runtime


def _timestamp(value: datetime | str | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _user_prompt_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    return "\n".join(item for item in part.content if isinstance(item, str))


def _turn_cost(turn: Turn) -> float | None:
    usage = json.loads(turn.usage)
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost)


def _saved_agent_step(
    steps: list[dict[str, Any]],
    content: str,
    timestamp: str,
    turn: Turn,
    *,
    details_hidden: bool = False,
) -> None:
    extra: dict[str, Any] = {"details_hidden": True} if details_hidden else {"turn_id": turn.id}
    steps.append({
        "step_id": len(steps) + 1,
        "timestamp": timestamp,
        "source": "agent",
        "model_name": turn.model,
        "message": content,
        "extra": extra,
    })


async def debug_trace(runtime: Runtime, user: User, session_id: str) -> dict[str, Any]:
    session = await runtime.database.session(user, session_id)
    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(
            select(Turn).where(Turn.session_id == session_id).order_by(Turn.created_at, Turn.id)
        ))
        messages = list(await db.scalars(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        ))

        from .message_sources import public_message
        public = {row.id: (await public_message(runtime.database, db, row))[0] for row in messages}
        own = {turn.actor_id: await runtime.database.same_person(db, user, turn.actor_id) for turn in turns}

    messages_by_turn: dict[str, list[Message]] = {}
    for message in messages:
        messages_by_turn.setdefault(message.turn_id, []).append(message)

    steps: list[dict[str, Any]] = []
    last_instructions: str | None = None
    total_cost_usd = 0.0
    has_cost = False

    def add_system(
        message: str,
        timestamp: datetime | str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "timestamp": _timestamp(timestamp),
            "source": "system",
            "message": message,
        }
        if extra is not None:
            step["extra"] = extra
        steps.append(step)

    def add_instructions(message: str, timestamp: datetime | str | None) -> None:
        nonlocal last_instructions
        if message == last_instructions:
            return
        last_instructions = message
        add_system(message, timestamp)

    for turn in turns:
        turn_messages = messages_by_turn.get(turn.id, [])
        user_message = next((row for row in turn_messages if row.role == "user"), None)
        user_step: dict[str, Any] | None = None
        if user_message is not None:
            user_step = {
                "step_id": len(steps) + 1,
                "timestamp": user_message.created_at,
                "source": "user",
                "message": public[user_message.id],
                "extra": {
                    "turn_id": turn.id,
                    "turn_status": turn.status,
                    "author": "requester" if own.get(user_message.author_id or "", False) else "teammate",
                },
            }
            steps.append(user_step)

        if own[turn.actor_id]:
            snapshot = await latest_turn_snapshot(runtime, turn.id)
            if snapshot is not None:
                tool_steps: dict[str, tuple[dict[str, Any], bool]] = {}
                first_request = True
                run_messages: list[ModelMessage] = [
                    message for message in snapshot.messages if is_turn_run(message.run_id, turn.id)
                ]
                for message in run_messages:
                    if isinstance(message, ModelRequest):
                        if message.instructions is not None:
                            add_instructions(message.instructions, message.timestamp)
                        user_parts: list[str] = []
                        for part in message.parts:
                            if isinstance(part, SystemPromptPart):
                                add_instructions(part.content, message.timestamp)
                            elif isinstance(part, UserPromptPart):
                                text = _user_prompt_text(part)
                                if first_request:
                                    user_parts.append(text)
                                else:
                                    steps.append({
                                        "step_id": len(steps) + 1,
                                        "timestamp": _timestamp(message.timestamp),
                                        "source": "user",
                                        "message": text,
                                        "extra": {"kind": "steering"},
                                    })
                            elif isinstance(part, (ToolReturnPart, RetryPromptPart)):
                                call_id = part.tool_call_id
                                issued = tool_steps.get(call_id) if call_id else None
                                if issued is None:
                                    if isinstance(part, RetryPromptPart):
                                        if private_tool(part.tool_name):
                                            add_system(
                                                f"[redacted: result of private tool {part.tool_name} "
                                                "is hidden in debug traces]",
                                                message.timestamp,
                                                {"kind": "retry", "redacted": True},
                                            )
                                        else:
                                            add_system(
                                                str(part.content), message.timestamp, {"kind": "retry"}
                                            )
                                    continue
                                agent_step, private = issued
                                private_result = private or private_tool(part.tool_name)
                                result_content = (
                                    f"[redacted: result of private tool {part.tool_name} "
                                    "is hidden in debug traces]"
                                    if private_result
                                    else str(part.content)[:30_000]
                                )
                                failed = part.part_kind == "retry-prompt" or getattr(part, "outcome", None) == "failed"
                                observation = agent_step.setdefault("observation", {"results": []})
                                result: dict[str, Any] = {
                                    "source_call_id": call_id,
                                    "content": result_content,
                                    "extra": {"failed": failed},
                                }
                                if private_result:
                                    result["extra"]["redacted"] = True
                                observation["results"].append(result)
                        if first_request:
                            if user_step is not None and user_parts:
                                user_step["extra"]["prompt_received"] = "\n".join(user_parts)
                            first_request = False
                    elif isinstance(message, ModelResponse):
                        tool_calls = []
                        for part in message.parts:
                            if isinstance(part, ToolCallPart):
                                private = private_tool(part.tool_name)
                                invalid_arguments = False
                                if private:
                                    arguments: dict[str, Any] = {
                                        "redacted": (
                                            f"Arguments of private tool {part.tool_name} "
                                            "are hidden in debug traces."
                                        )
                                    }
                                else:
                                    try:
                                        arguments = part.args_as_dict(raise_if_invalid=True)
                                    except ValueError:
                                        arguments = {
                                            "raw_arguments": (
                                                part.args if isinstance(part.args, str) else str(part.args)
                                            )
                                        }
                                        invalid_arguments = True
                                call: dict[str, Any] = {
                                    "tool_call_id": part.tool_call_id,
                                    "function_name": part.tool_name,
                                    "arguments": arguments,
                                }
                                if private:
                                    call["extra"] = {"redacted": True}
                                elif invalid_arguments:
                                    call["extra"] = {"invalid_arguments": True}
                                tool_calls.append(call)

                        usage = message.usage
                        metrics = {
                            "prompt_tokens": usage.input_tokens,
                            "completion_tokens": usage.output_tokens,
                            "cached_tokens": usage.cache_read_tokens,
                        }
                        step: dict[str, Any] = {
                            "step_id": len(steps) + 1,
                            "timestamp": _timestamp(message.timestamp),
                            "source": "agent",
                            "message": "".join(
                                part.content for part in message.parts if isinstance(part, TextPart)
                            ),
                            "metrics": metrics,
                            "extra": {"run_id": message.run_id, "turn_id": turn.id},
                        }
                        if message.model_name is not None:
                            step["model_name"] = message.model_name
                        reasoning = "".join(
                            part.content for part in message.parts if isinstance(part, ThinkingPart)
                        )
                        if reasoning:
                            step["reasoning_content"] = reasoning
                        if tool_calls:
                            step["tool_calls"] = tool_calls
                        steps.append(step)
                        for part in message.parts:
                            if isinstance(part, ToolCallPart):
                                tool_steps[part.tool_call_id] = (step, private_tool(part.tool_name))
            else:
                assistant_message = next(
                    (row for row in reversed(turn_messages) if row.role == "assistant"), None
                )
                if assistant_message is not None:
                    _saved_agent_step(
                        steps,
                        runtime.security.unseal(assistant_message.content).decode(),
                        assistant_message.created_at,
                        turn,
                    )
        else:
            assistant_message = next(
                (row for row in reversed(turn_messages) if row.role == "assistant"), None
            )
            if assistant_message is not None:
                _saved_agent_step(
                    steps,
                    runtime.security.unseal(assistant_message.content).decode(),
                    assistant_message.created_at,
                    turn,
                    details_hidden=True,
                )

        if turn.error:
            steps.append({
                "step_id": len(steps) + 1,
                "timestamp": turn.finished_at or turn.created_at,
                "source": "system",
                "message": turn.error,
                "extra": {"kind": "error", "turn_id": turn.id},
            })
        if (cost := _turn_cost(turn)) is not None:
            total_cost_usd += cost
            has_cost = True

    final_metrics: dict[str, int | float] = {
        "total_prompt_tokens": sum(
            step.get("metrics", {}).get("prompt_tokens", 0) for step in steps
        ),
        "total_completion_tokens": sum(
            step.get("metrics", {}).get("completion_tokens", 0) for step in steps
        ),
        "total_cached_tokens": sum(
            step.get("metrics", {}).get("cached_tokens", 0) for step in steps
        ),
        "total_steps": len(steps),
    }
    if has_cost:
        final_metrics["total_cost_usd"] = total_cost_usd

    return {
        "schema_version": "ATIF-v1.8",
        "session_id": session.id,
        "agent": {
            "name": "moyai",
            "version": version("moyai-pydantic"),
            "model_name": session.model,
        },
        "steps": steps,
        "notes": (
            f"Moyai debug trace for session {session.title}: user messages, the agent's reasoning, "
            "tool calls and tool results in order. Hand it to Moyai with what you expected to debug "
            "where it went wrong. Arguments and results marked redacted were hidden for privacy, not empty."
        ),
        "final_metrics": final_metrics,
        "extra": {
            "exported_at": datetime.now(UTC).isoformat(),
            "session_title": session.title,
            "turn_count": len(turns),
        },
    }
