"""Requester-scoped work presentation, separate from root answer streaming.

Execution, retries and histories remain owned by the native SDK. This reader
uses its existing lineage, lifecycle events and public response snapshots; it
never republishes a child's text as the parent's answer.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Literal, TypedDict

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai_harness.step_persistence import RunRecord, StepEvent
from sqlalchemy import select

from .delegation_models import DelegatedSession, DelegatedTurn
from .models import Session, Turn
from .presentation import redact
from .progress import progress_secrets, turn_progress
from .workspace import agent_labels, run_lineage

if TYPE_CHECKING:
    from .runtime import Runtime

RUN_LIMIT = 24
TOOL_LIMIT = 80
MESSAGE_LIMIT = 80
TEXT_LIMIT = 32_000
CACHE_LIMIT = 96


class ToolProgress(TypedDict):
    id: str
    name: str
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: str
    finished_at: str | None


class AgentProgress(TypedDict):
    id: str
    session_id: str | None
    parent_id: str | None
    parent_name: str | None
    name: str
    avatar: int
    depth: int
    status: str
    created_at: str
    updated_at: str
    messages: list[dict[str, str]]
    tools: list[ToolProgress]
    messages_limited: bool
    tools_limited: bool


class WorkProgressView(TypedDict):
    messages: list[dict[str, str]]
    tools: list[ToolProgress]
    subagents: list[AgentProgress]
    tools_limited: bool
    subagents_limited: bool


def stamp(value: datetime) -> str:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def tools(events: list[StepEvent], *, terminal: bool, secrets: list[str]) -> tuple[list[ToolProgress], bool]:
    calls: dict[str, ToolProgress] = {}
    for event in events:
        if (
            event.kind not in {"tool_call_started", "tool_call_completed", "tool_call_failed"}
            or not event.tool_call_id or not event.tool_name
        ):
            continue
        item = calls.setdefault(event.tool_call_id, {
            "id": event.tool_call_id, "name": str(redact(event.tool_name, secrets)),
            "status": "running", "created_at": stamp(event.timestamp), "finished_at": None,
        })
        item["status"] = (
            "completed" if event.kind == "tool_call_completed"
            else "failed" if event.kind == "tool_call_failed" else "running"
        )
        item["finished_at"] = None if item["status"] == "running" else stamp(event.timestamp)
    if terminal:
        for item in calls.values():
            if item["status"] == "running":
                item["status"] = "interrupted"
    values = list(calls.values())
    return values[-TOOL_LIMIT:], len(values) > TOOL_LIMIT


@dataclass
class _PublicSnapshot:
    revision: tuple[int, str, int, str]
    expires: float
    messages: list[dict[str, str]]
    limited: bool


class WorkProgress:
    """Small public-text cache; every request still validates current authority."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._snapshots: OrderedDict[tuple[str, str], _PublicSnapshot] = OrderedDict()

    def clear(self) -> None:
        self._snapshots.clear()

    async def _delegated(self, turn: Turn, *, limit: int) -> tuple[list[AgentProgress], bool]:
        """Read only the assignment's generated turns, never later private chat."""
        queue: list[tuple[str, str | None, int]] = [(turn.id, None, 0)]
        found: list[AgentProgress] = []
        visible_sessions: list[str] = []
        seen: set[str] = set()
        limited = False
        while queue and len(found) < limit:
            parent_turn, parent_name, depth = queue.pop(0)
            async with self.runtime.database.sessions() as db:
                links = list(await db.scalars(select(DelegatedSession).join(
                    Session, Session.id == DelegatedSession.session_id,
                ).where(
                    DelegatedSession.parent_turn_id == parent_turn,
                    DelegatedSession.actor_id == turn.actor_id,
                    Session.owner_id == turn.actor_id, Session.deleted.is_(False),
                ).order_by(DelegatedSession.created_at).limit(limit + 1)))
            for link in links:
                if len(found) >= limit:
                    limited = True
                    break
                if link.session_id in seen:
                    continue
                seen.add(link.session_id)
                async with self.runtime.database.sessions() as db:
                    generated = list(await db.scalars(select(Turn).join(
                        DelegatedTurn, DelegatedTurn.turn_id == Turn.id,
                    ).where(
                        DelegatedTurn.session_id == link.session_id,
                        Turn.session_id == link.session_id, Turn.actor_id == turn.actor_id,
                    ).order_by(Turn.created_at.desc()).limit(17)))
                history_limited = len(generated) > 16
                generated = list(reversed(generated[:16]))
                if not generated:
                    continue
                messages, activity = [], []
                tools_limited = False
                updated = link.created_at
                for child in generated:
                    child_messages = await turn_progress(self.runtime, child)
                    messages.extend(child_messages)
                    events = await self.runtime.stores.steps.list_events(run_id=child.id)
                    child_tools, clipped = tools(
                        events, terminal=child.status in {"completed", "failed", "stopped", "resumed"},
                        secrets=progress_secrets(self.runtime, child),
                    )
                    activity.extend({**item, "id": f"{child.id}:{item['id']}"} for item in child_tools)
                    tools_limited = tools_limited or clipped
                    updated = max(updated, child.finished_at or child.created_at,
                                  stamp(events[-1].timestamp) if events else "")
                    if child.result and child.result_accepted and child.status in {"completed", "saving", "resumed"}:
                        messages.append({
                            "id": f"result:{child.id}", "phase": "final_answer", "created_at": updated,
                            "text": str(redact(
                                self.runtime.security.unseal(child.result).decode(),
                                progress_secrets(self.runtime, child),
                            )),
                        })
                    if depth < 12:
                        queue.append((child.id, link.name, depth + 1))
                messages_limited = history_limited or len(messages) > MESSAGE_LIMIT
                recent, remaining = [], TEXT_LIMIT
                for message in reversed(messages[-MESSAGE_LIMIT:]):
                    if remaining <= 0:
                        messages_limited = True
                        break
                    text = message["text"][:remaining]
                    messages_limited = messages_limited or len(text) < len(message["text"])
                    recent.append({**message, "text": text})
                    remaining -= len(text)
                visible_sessions.append(link.session_id)
                found.append({
                    "id": link.initial_turn_id, "session_id": link.session_id,
                    "parent_id": parent_turn, "parent_name": parent_name,
                    "name": link.name, "avatar": link.avatar, "depth": depth,
                    "status": generated[-1].status, "created_at": link.created_at, "updated_at": updated,
                    "messages": list(reversed(recent)), "tools": activity[-TOOL_LIMIT:],
                    "messages_limited": messages_limited,
                    "tools_limited": tools_limited or len(activity) > TOOL_LIMIT,
                })
        # A child can be deleted while its asynchronous native reads are pending.
        async with self.runtime.database.sessions() as db:
            allowed = set(await db.scalars(select(Session.id).where(
                Session.id.in_(visible_sessions), Session.owner_id == turn.actor_id, Session.deleted.is_(False),
            )))
        return [item for item in found if item["session_id"] in allowed], limited or bool(queue)

    async def _messages(
        self, turn: Turn, record: RunRecord, events: list[StepEvent], secrets: list[str],
    ) -> tuple[list[dict[str, str]], bool]:
        last = events[-1] if events else None
        revision = (
            len(events), last.kind if last else "", last.step_index if last else -1,
            stamp(last.timestamp) if last else "",
        )
        key = (turn.id, record.run_id)
        cached = self._snapshots.get(key)
        # A model-completed event can precede the saved frontier. Re-read that
        # boundary until a subsequent native event confirms that it has advanced.
        if (
            cached and cached.revision == revision and cached.expires > monotonic()
            and (last is None or last.kind != "model_request_completed")
        ):
            self._snapshots.move_to_end(key)
            return [
                {**message, "text": str(redact(message["text"], secrets))}
                for message in cached.messages
            ], cached.limited
        snapshot = await self.runtime.stores.steps.latest_snapshot(
            run_id=record.run_id, include_interrupted=True,
        )
        messages: list[dict[str, str]] = []
        for message in snapshot.messages if snapshot else []:
            if not isinstance(message, ModelResponse) or message.run_id != record.run_id:
                continue
            has_tools = any(isinstance(part, ToolCallPart) for part in message.parts)
            for index, part in enumerate(message.parts):
                if not isinstance(part, TextPart) or not part.content.strip():
                    continue
                phase = (part.provider_details or {}).get("phase")
                if phase not in {None, "commentary", "final_answer"}:
                    continue
                messages.append({
                    "id": f"{record.run_id}:{message.provider_response_id or stamp(message.timestamp)}:{index}",
                    "text": str(redact(part.content, secrets)),
                    "phase": phase or ("commentary" if has_tools else "final_answer"),
                    "created_at": stamp(message.timestamp),
                })
        # Retain recent public updates without caching model history, tool
        # arguments/results, inherited messages, media or private reasoning.
        limited = len(messages) > MESSAGE_LIMIT
        selected: list[dict[str, str]] = []
        remaining = TEXT_LIMIT
        for message in reversed(messages[-MESSAGE_LIMIT:]):
            if remaining <= 0:
                limited = True
                break
            text = message["text"]
            if len(text) > remaining:
                text = text[:remaining]
                limited = True
            selected.append({**message, "text": text})
            remaining -= len(text)
        selected.reverse()
        self._snapshots[key] = _PublicSnapshot(revision, monotonic() + 60, selected, limited)
        self._snapshots.move_to_end(key)
        while len(self._snapshots) > CACHE_LIMIT:
            self._snapshots.popitem(last=False)
        return selected, limited

    async def read(self, turn: Turn) -> WorkProgressView:
        secrets = progress_secrets(self.runtime, turn)
        messages = await turn_progress(self.runtime, turn)
        lineage, limited = await run_lineage(self.runtime, [turn], limit=RUN_LIMIT)
        labels = await agent_labels(self.runtime, turn.session_id, [record for record, _, _ in lineage])
        semaphore = asyncio.Semaphore(4)

        async def child(record: RunRecord, depth: int) -> AgentProgress:
            async with semaphore:
                events = await self.runtime.stores.steps.list_events(run_id=record.run_id)
                terminal = next(
                    (event.kind for event in reversed(events) if event.kind in {"run_completed", "run_failed"}),
                    None,
                )
                status = (
                    "completed" if terminal == "run_completed" else "failed" if terminal
                    else "running" if turn.status in {"starting", "running", "saving"}
                    else "waiting" if turn.status == "waiting"
                    else "stopping" if turn.status == "stopping" else "interrupted"
                )
                public_messages, messages_limited = await self._messages(turn, record, events, secrets)
                activity, tools_limited = tools(
                    events, terminal=status in {"completed", "failed", "interrupted"}, secrets=secrets,
                )
            label = labels[record.run_id]
            parent = labels.get(record.parent_run_id or "")
            return {
                "id": record.run_id, "session_id": None, "parent_id": record.parent_run_id,
                "parent_name": parent.name if parent else None,
                "name": label.name, "avatar": label.avatar, "depth": depth, "status": status,
                "created_at": stamp(record.started_at),
                "updated_at": stamp(events[-1].timestamp if events else record.started_at),
                "messages": public_messages, "tools": activity,
                "messages_limited": messages_limited, "tools_limited": tools_limited,
            }

        root_events = await self.runtime.stores.steps.list_events(run_id=turn.id)
        activity, tools_limited = tools(
            root_events, terminal=turn.status in {"completed", "failed", "stopped", "resumed"}, secrets=secrets,
        )
        children = list(await asyncio.gather(*(child(record, depth) for record, _, depth in lineage)))
        delegated, delegated_limited = await self._delegated(turn, limit=max(0, RUN_LIMIT - len(children)))
        return {
            "messages": messages, "tools": activity, "subagents": children + delegated,
            "tools_limited": tools_limited, "subagents_limited": limited or delegated_limited,
        }
