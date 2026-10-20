"""Bound Slack audience lookups independently of browser/event frequency."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic
from typing import Any

from app.connector_errors import ConnectorError
from fastapi import HTTPException

AUDIENCE_TTL = 60
MAX_ENTRIES = 512
Key = tuple[str, str, str, str, str]
Scope = tuple[str, str, str]


class SlackAudienceReads:
    """Share provider snapshots, never a caller's authorization decision.

    Identity, installation authority and local task permissions are checked by
    the caller on every use. Slack-only membership changes are observed within
    one minute. Expired snapshots are never used after a provider failure.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic):
        self.clock = clock
        self.snapshots: OrderedDict[Key, tuple[float, dict[str, Any]]] = OrderedDict()
        self.pending: dict[Key, asyncio.Task[dict[str, Any]]] = {}
        self.cooldowns: OrderedDict[Scope, float] = OrderedDict()

    def cached(self, installed, method: str, channel: str, cursor: str = "") -> dict[str, Any]:
        """A transaction may consume fresh provider facts, never start provider I/O."""
        key = (installed["team_id"], installed["authority"], method, channel, cursor)
        saved = self.snapshots.get(key)
        if saved is not None and saved[0] > self.clock():
            self.snapshots.move_to_end(key)
            return saved[1]
        raise HTTPException(503, "Slack access needs a fresh verification. Refresh to retry.")

    def denied(self, installed, channel: str) -> None:
        """Retain a provider denial as data, not an authorization decision."""
        key = (installed["team_id"], installed["authority"], "conversations.info", channel, "")
        self.snapshots[key] = (self.clock() + AUDIENCE_TTL, {"ok": False})
        self.snapshots.move_to_end(key)
        while len(self.snapshots) > MAX_ENTRIES:
            self.snapshots.popitem(last=False)

    async def read(self, slack, installed, method: str, channel: str, cursor: str = "") -> dict[str, Any]:
        scope = (installed["team_id"], installed["authority"], method)
        key = (*scope, channel, cursor)
        saved = self.snapshots.get(key)
        if saved is not None:
            if saved[0] > self.clock():
                self.snapshots.move_to_end(key)
                return saved[1]
            del self.snapshots[key]
        remaining = self.cooldowns.get(scope, 0) - self.clock()
        if remaining > 0:
            raise ConnectorError("Slack audience lookup is cooling down.", code="rate_limited",
                                 status_code=429, retryable=True, retry_after=math.ceil(remaining))
        self.cooldowns.pop(scope, None)
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(slack, scope, key, channel, cursor))
            self.pending[key] = task
            task.add_done_callback(lambda finished: self._finished(key, finished))
        # Closing one browser tab must not cancel a check shared with other tabs.
        return await asyncio.shield(task)

    def _finished(self, key: Key, task: asyncio.Task[dict[str, Any]]) -> None:
        if self.pending.get(key) is task:
            del self.pending[key]
        if not task.cancelled():
            task.exception()  # Consume a failed lookup even if every reader disconnected.

    async def _fetch(self, slack, scope: Scope, key: Key, channel: str, cursor: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(8):
                token = await slack.connectors.slack_bot_token()
                params: dict[str, Any] = {"channel": channel}
                if scope[2] == "conversations.members":
                    params.update(cursor=cursor, limit=200)
                response = await slack.connectors.request("GET", "https://slack.com/api/" + scope[2],
                    headers={"Authorization": "Bearer " + token}, params=params)
        except ConnectorError as error:
            if error.status_code == 429 or error.code == "rate_limited":
                delay = error.retry_after
                delay = math.ceil(delay) if isinstance(delay, (int, float)) and math.isfinite(delay) and delay > 0 else 30
                self.cooldowns[scope] = self.clock() + delay
                self.cooldowns.move_to_end(scope)
                while len(self.cooldowns) > MAX_ENTRIES:
                    self.cooldowns.popitem(last=False)
            raise
        # Retain only audience metadata, not channel topics or other Slack content.
        if scope[2] == "conversations.info":
            info = response.get("channel", {})
            result = {"ok": response.get("ok"), "channel": {
                name: info.get(name) for name in ("id", "is_ext_shared", "is_private", "is_channel")
            }}
        else:
            result = {"ok": response.get("ok"), "members": response.get("members", []),
                      "response_metadata": {"next_cursor": (response.get("response_metadata") or {}).get("next_cursor", "")}}
        if result["ok"]:
            self.snapshots[key] = (self.clock() + AUDIENCE_TTL, result)
            self.snapshots.move_to_end(key)
            while len(self.snapshots) > MAX_ENTRIES:
                self.snapshots.popitem(last=False)
        return result
