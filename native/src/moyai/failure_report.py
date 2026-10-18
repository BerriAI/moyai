from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentchat.models import UploadFile

from .debug_trace import debug_trace
from .models import Turn, User

if TYPE_CHECKING:
    from .runtime import Runtime


@dataclass(frozen=True)
class FailureReport:
    text: str
    debug_log: UploadFile

    @classmethod
    async def for_turn(cls, runtime: Runtime, turn: Turn) -> FailureReport:
        text = (
            f"This task stopped before it completed: {turn.error}\nDebug log attached."
            if turn.error else "This task stopped before it completed.\nDebug log attached."
        )
        try:
            async with runtime.database.sessions() as db:
                actor = await db.get(User, turn.actor_id)
            if actor is None:
                raise ValueError("The task requester no longer exists.")
            trace = await debug_trace(runtime, actor, turn.session_id)
            content = json.dumps(trace).encode()
        except Exception as exc:  # noqa: BLE001  # [BLE001] Export failures need fallback JSON.
            content = json.dumps({
                "session_id": turn.session_id,
                "turn_id": turn.id,
                "error": turn.error,
                "trace_export_error": str(exc),
            }).encode()
        return cls(
            text=text,
            debug_log=UploadFile(
                filename=f"moyai-debug-{turn.id}.json",
                content=content,
                title="Moyai debug log",
            ),
        )
