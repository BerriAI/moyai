"""Nested debug activity projected from the existing, authorized execution stores.

Session readers get the same public text and tool payloads as WorkProgress.
Only a turn's requester gets recorded reasoning. Raw provider/run metadata,
user prompts and system instructions never enter this public projection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from sqlalchemy import select

from .commentary import public_text_parts
from .delegation_models import DelegatedSession, DelegatedTurn
from .history_handoff import turn_run_ids
from .models import Event, Session, Turn
from .presentation import private_tool, redact
from .progress import progress_secrets
from .runtime import Runtime


def stamp(value: datetime | str) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


def _sort(items: list[dict[str, Any]]) -> None:
    # Stable ties retain original message-part and persisted-event ordering.
    items.sort(key=lambda item: item["timestamp"])
    for item in items:
        if "children" in item:
            _sort(item["children"])


def _call(part: ToolCallPart | NativeToolCallPart, timestamp: str) -> dict[str, Any]:
    private = private_tool(part.tool_name)
    arguments: Any = {"redacted": f"Arguments of private tool {part.tool_name} are hidden in debug traces."}
    invalid = False
    if not private:
        try:
            arguments = part.args_as_dict(raise_if_invalid=True)
        except ValueError:
            arguments, invalid = {"raw_arguments": part.args}, True
    item: dict[str, Any] = {
        "kind": "tool_call", "timestamp": timestamp, "tool_call_id": part.tool_call_id,
        "tool_name": part.tool_name, "arguments": arguments, "redacted": private, "children": [],
    }
    if invalid:
        item["invalid_arguments"] = True
    return item


def _nested_tools(metadata: Any, timestamp: str) -> list[dict[str, Any]]:
    # CodeMode stores sub-calls here. All other metadata (provider state,
    # credential context, etc.) stays private. Validate serialized dataclasses
    # too: snapshot persistence can deserialize arbitrary metadata as dicts.
    if not isinstance(metadata, dict) or metadata.get("code_mode") is not True:
        return []
    calls, results = metadata.get("tool_calls"), metadata.get("tool_returns")
    if not isinstance(calls, dict) or not isinstance(results, dict):
        return []
    nested = []
    for call_id, value in calls.items():
        try:
            part = TypeAdapter(ToolCallPart).validate_python(value)
            if part.tool_call_id != call_id:
                continue
            item = _call(part, timestamp)
            item["timestamp_source"] = "parent_call"
            if call_id in results:
                result = TypeAdapter(ToolReturnPart).validate_python(results[call_id])
                if result.tool_call_id == call_id and result.tool_name == part.tool_name:
                    projected = tool_result_activity(result, stamp(result.timestamp), call_timestamp=timestamp)
                    item["children"].append(projected)
                    _redact_wrapper(item, projected)
            nested.append(item)
        except (ValidationError, TypeError):
            continue
    return nested


def _redact_wrapper(call: dict[str, Any], result: dict[str, Any]) -> None:
    if result.get("children") and result["redacted"]:
        call.update(arguments={"redacted": "Code arguments include private subtool activity."}, redacted=True)


def tool_result_activity(
    part: ToolReturnPart | RetryPromptPart | NativeToolReturnPart, timestamp: str,
    *, call_timestamp: str | None = None,
) -> dict[str, Any]:
    private = private_tool(part.tool_name)
    nested = [] if private else _nested_tools(getattr(part, "metadata", None), call_timestamp or timestamp)
    # A code-mode wrapper can echo the results of its private subtools.
    nested_private = any(item["redacted"] or any(child.get("redacted") for child in item["children"]) for item in nested)
    result: dict[str, Any] = {
        "kind": "tool_result", "timestamp": timestamp, "tool_call_id": part.tool_call_id,
        "tool_name": part.tool_name,
        "content": (
            f"[redacted: result of private tool {part.tool_name} is hidden in debug traces]"
            if private else "[redacted: code output includes private subtool activity]" if nested_private
            else str(part.content) if isinstance(part, RetryPromptPart) else part.model_response_str()
        ),
        "failed": isinstance(part, RetryPromptPart) or part.outcome == "failed",
        "outcome": "retry" if isinstance(part, RetryPromptPart) else part.outcome,
        "redacted": private or nested_private,
    }
    if nested:
        result["children"] = nested
    return result


def _snapshot_activity(messages, run_id: str, *, include_private: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    current_response = False
    for message in messages:
        if isinstance(message, ModelResponse):
            current_response = message.run_id == run_id
            if not current_response:
                continue
            timestamp = stamp(message.timestamp)
            response: dict[str, Any] = {
                "kind": "model_response", "timestamp": timestamp,
                "run_id": run_id, "children": [],
                "record_id": message.provider_response_id or hashlib.sha256(ModelMessagesTypeAdapter.dump_json([message])).hexdigest(),
            }
            if message.model_name:
                response["model_name"] = message.model_name
            public_text = {index: (text, phase) for index, text, phase in public_text_parts(message)}
            for index, part in enumerate(message.parts):
                item: dict[str, Any] | None = None
                if index in public_text:
                    text, phase = public_text[index]
                    item = {"kind": phase, "timestamp": timestamp, "content": text}
                elif isinstance(part, ThinkingPart) and include_private and part.content:
                    item = {"kind": "reasoning", "timestamp": timestamp, "content": part.content}
                elif isinstance(part, (ToolCallPart, NativeToolCallPart)):
                    item = _call(part, timestamp)
                    calls[part.tool_call_id] = item
                elif isinstance(part, NativeToolReturnPart):
                    item = tool_result_activity(part, timestamp)
                if item is not None:
                    item["part_index"] = index
                    response["children"].append(item)
            if response["children"]:
                items.append(response)
        elif isinstance(message, ModelRequest) and current_response:
            for part in message.parts:
                if not isinstance(part, (ToolReturnPart, RetryPromptPart)):
                    continue
                # A request may contain inherited results. Match both identifiers,
                # and never attach an unrelated call with the same provider ID.
                call = calls.get(part.tool_call_id or "")
                if call is None or call["tool_name"] != part.tool_name:
                    continue
                result = tool_result_activity(part, stamp(part.timestamp), call_timestamp=call["timestamp"])
                if call["redacted"]:
                    result.update(content=f"[redacted: result of private tool {part.tool_name} is hidden in debug traces]", redacted=True)
                call["children"].append(result)
                _redact_wrapper(call, result)
    return items


def _merge(items: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> None:
    # Snapshots repeat conversation history and later add tool returns. Keep
    # each model response/part once while retaining every recorded child.
    def identity(item):
        return (item["kind"], item["timestamp"], item.get("record_id"), item.get("part_index"), item.get("tool_call_id"))

    positions = {identity(item): item for item in items}
    for item in incoming:
        previous = positions.get(identity(item))
        if previous is None:
            items.append(item)
            positions[identity(item)] = item
        else:
            children = previous.pop("children", [])
            previous.update(item)
            if "children" in item or children:
                _merge(children, item.get("children", []))
                previous["children"] = children
                if previous["kind"] == "tool_call":
                    for result in children:
                        _redact_wrapper(previous, result)


def redact_trace_calls(steps: list[dict[str, Any]], activity: list[dict[str, Any]]) -> None:
    # An earlier snapshot can reveal private subtools after the latest snapshot
    # has lost their results. Redact the compatible ATIF copies from that union.
    private_calls: set[tuple[str, str]] = set()

    def collect(items, run_id=None):
        for item in items:
            current_run = item.get("run_id", run_id)
            if item["kind"] == "tool_call" and item["redacted"] and current_run:
                private_calls.add((current_run, item["tool_call_id"]))
            collect(item.get("children", []), current_run)

    collect(activity)
    for step in steps:
        for call in step.get("tool_calls", []):
            if (step.get("extra", {}).get("run_id"), call["tool_call_id"]) not in private_calls:
                continue
            if not call.get("extra", {}).get("redacted"):
                call["arguments"] = {"redacted": "Code arguments include private subtool activity."}
                call.setdefault("extra", {})["redacted"] = True
            for result in step.get("observation", {}).get("results", []):
                if result["source_call_id"] == call["tool_call_id"] and not result.get("extra", {}).get("redacted"):
                    result["content"] = "[redacted: result includes private subtool activity]"
                    result.setdefault("extra", {})["redacted"] = True


async def prune_delegations(runtime: Runtime, trees: list[list[dict[str, Any]]], owners: dict[str, str]) -> None:
    """Recheck all delegated sources after the entire export's asynchronous reads."""
    if not owners:
        return
    async with runtime.database.sessions() as db:
        allowed = {row.id for row in await db.scalars(select(Session).where(
            Session.id.in_(owners), Session.deleted.is_(False),
        )) if row.owner_id == owners[row.id]}

    def prune(items):
        items[:] = [item for item in items if item["kind"] != "delegation" or item["session_id"] in allowed]
        for item in items:
            prune(item.get("children", []))

    for items in trees:
        prune(items)


async def trace_activity(runtime: Runtime, turn: Turn, *, include_private: bool,
                         delegated_owners: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Walk only server-recorded descendants and assignment-generated turns.

    Each run retains its own timeline. A missing snapshot is explicit; lifecycle
    records remain useful for interrupted runs and tools that never returned.
    """
    delegated_owners = {} if delegated_owners is None else delegated_owners
    seen_runs: set[str] = set()
    seen_turns: set[str] = set()

    async def run_activity(run_id: str, source: Turn, *, parent_id: str | None = None,
                           started_at: datetime | str | None = None) -> dict[str, Any] | None:
        if run_id in seen_runs:
            return None
        seen_runs.add(run_id)
        events = [event for event in await runtime.stores.steps.list_events(run_id=run_id)
                  if event.run_id == run_id]
        snapshots = await runtime.stores.steps.list_snapshots(run_id=run_id, include_interrupted=True)
        snapshot = snapshots[-1] if snapshots else None
        items: list[dict[str, Any]] = []
        for saved in snapshots:
            _merge(items, _snapshot_activity(saved.messages, run_id, include_private=include_private))
        for event in events:
            item: dict[str, Any] = {
                "kind": event.kind, "timestamp": stamp(event.timestamp),
                "run_id": run_id, "step_index": event.step_index,
            }
            if event.tool_call_id:
                item["tool_call_id"] = event.tool_call_id
            if event.tool_name:
                item["tool_name"] = event.tool_name
            # Error strings can contain a private tool's inputs or provider
            # context; use the sanitized failure projection in the outer trace.
            items.append(item)
        for child in await runtime.stores.steps.list_runs(parent_run_id=run_id):
            if child.parent_run_id != run_id:
                continue
            nested = await run_activity(child.run_id, source, parent_id=run_id, started_at=child.started_at)
            if nested is not None:
                items.append(nested)
        _sort(items)
        return {
            "kind": "run", "run_id": run_id, "turn_id": source.id,
            "parent_run_id": parent_id,
            "timestamp": stamp(started_at or (items[0]["timestamp"] if items else source.created_at)),
            "snapshot_status": snapshot.state if snapshot else "unavailable",
            "retained_snapshots": len(snapshots),
            "children": redact(items, progress_secrets(runtime, source)),
        }

    async def turn_activity(source: Turn) -> list[dict[str, Any]]:
        if source.id in seen_turns:
            return []
        seen_turns.add(source.id)
        roots = []
        for run_id in await turn_run_ids(runtime, source.id):
            item = await run_activity(run_id, source)
            if item is not None:
                roots.append(item)
        if not roots:
            return []
        async with runtime.database.sessions() as db:
            commentary = list(await db.scalars(select(Event).where(
                Event.turn_id == source.id, Event.kind == "commentary",
            ).order_by(Event.id)))
            links = list(await db.scalars(select(DelegatedSession).join(
                Session, Session.id == DelegatedSession.session_id,
            ).where(
                DelegatedSession.parent_turn_id == source.id, DelegatedSession.actor_id == turn.actor_id,
                Session.owner_id == turn.actor_id, Session.deleted.is_(False),
            ).order_by(DelegatedSession.created_at)))
        # Durable commentary survives model-history compaction. Avoid repeating
        # a projection already present at the identical response timestamp.
        text_keys = {(part["timestamp"], part.get("content"))
                     for root in roots for response in root["children"] if response["kind"] == "model_response"
                     for part in response["children"] if part["kind"] == "commentary"}
        for row in commentary:
            value = json.loads(runtime.security.unseal(row.content))
            timestamp = stamp(value.get("created_at") or row.created_at)
            if (timestamp, value["text"]) not in text_keys:
                roots[0]["children"].append({
                    "kind": "commentary", "timestamp": timestamp, "content": value["text"],
                    "record_id": value["id"],
                })
        for link in links:
            async with runtime.database.sessions() as db:
                generated = list(await db.scalars(select(Turn).join(
                    DelegatedTurn, DelegatedTurn.turn_id == Turn.id,
                ).where(
                    DelegatedTurn.session_id == link.session_id, Turn.session_id == link.session_id,
                    Turn.actor_id == turn.actor_id,
                ).order_by(Turn.created_at, Turn.id)))
            nested = []
            for child in generated:
                nested.extend(await turn_activity(child))
            # Match WorkProgress: later private follow-ups are never included,
            # and a deletion during asynchronous reads withdraws the child.
            async with runtime.database.sessions() as db:
                allowed = await db.scalar(select(Session.id).where(
                    Session.id == link.session_id, Session.owner_id == turn.actor_id, Session.deleted.is_(False),
                ))
            if allowed:
                delegated_owners[link.session_id] = turn.actor_id
                roots[0]["children"].append({
                    "kind": "delegation", "timestamp": stamp(link.created_at),
                    "parent_turn_id": source.id, "tool_call_id": link.tool_call_id,
                    "session_id": link.session_id,
                    "name": link.name, "children": nested,
                })
        _sort(roots)
        return redact(roots, progress_secrets(runtime, source))

    result = await turn_activity(turn)
    await prune_delegations(runtime, [result], delegated_owners)
    return result
