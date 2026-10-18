"""Reconcile Slack's expiring native status without posting chat messages.

The coordinator owns this lane independently of answer delivery. Persist attempts
before sending: an ambiguous status is safe to retry, including clearing it after
restart. Message delivery receipts and their non-replayable claims stay untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from app.db import database as source_database
from fastapi import HTTPException
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import Delivery, ProductTurnAuthority, Session, SlackActivity, SlackThread, Turn, User, new_id
from .product_identity import ProductGrant
from .product_slack_models import ProductSlackBinding

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)
STATUSES = {
    "queued": "is getting ready…",
    "starting": "is opening the workspace…",
    "running": "is working…",
    "saving": "is saving the work…",
}
REFRESH_SECONDS = 60
REQUEST_SECONDS = 3
CLAIM_SECONDS = 15


@dataclass(frozen=True)
class Target:
    channel: str
    thread_ts: str
    status: str
    delivery_version: str


class SlackActivityService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.wake = asyncio.Event()

    async def target(self, thread_id: str) -> Target | None:
        """Recheck current work and the original destination; never expose focus text."""
        runtime, product = self.runtime, self.runtime.product_slack
        bot = None
        if product:
            from .product_slack import SlackInstallationUnavailable
            try:
                bot = await source_database(product.installation)
            except SlackInstallationUnavailable:
                return None
        async with runtime.database.sessions() as db:
            thread = await db.get(SlackThread, thread_id)
            if thread is None:
                return None
            session = await db.get(Session, thread.session_id)
            if session is None:
                return None
            turn = await db.get(Turn, session.active_turn) if session.active_turn else await db.scalar(
                select(Turn).where(
                    Turn.session_id == session.id, Turn.status == "queued",
                    ~Turn.workflow_id.startswith("moyai-slack-control-"),
                ).order_by(Turn.created_at, Turn.id).limit(1),
            )
            actor = await db.get(User, turn.actor_id) if turn else None
            status = STATUSES.get(turn.status, "") if turn and actor and actor.enabled and not session.deleted else ""
            if turn and turn.workflow_id.startswith("moyai-slack-control-"):
                status = ""
            # An attempted reply can clear Slack's status even if its ack is lost.
            # The sent count also detects replies that finish after status was set.
            deliveries = (await db.execute(select(
                func.max(Delivery.claimed_at),
                func.sum(case((Delivery.status == "sent", 1), else_=0)),
                func.sum(case((Delivery.status == "sending", 1), else_=0)),
            ).where(Delivery.session_id == session.id))).one()
            version = hashlib.sha256(json.dumps(tuple(deliveries)).encode()).hexdigest()
            admitted = await db.get(ProductTurnAuthority, turn.id) if product and turn else None
            if product:
                binding = await db.get(ProductSlackBinding, session.id)
                if (
                    not bot or not binding or binding.connection_authority != bot["authority"]
                    or (binding.team_id, binding.channel_id, binding.thread_ts)
                    != (thread.team_id, thread.channel_id, thread.thread_ts)
                    or thread.team_id != bot["team_id"]
                    or not product.settings.slack_bot_enabled
                    or not product.settings.slack_thread_chat_enabled
                ):
                    return None
                if (
                    binding.paused or session.visibility != "shared" or session.surface != "slack"
                    or admitted is None
                    or (thread.channel_id.startswith("D") and not product.settings.slack_dm_enabled)
                ):
                    status = ""
            elif thread.team_id != runtime.settings.slack_team_id:
                return None
        if product and status and admitted:
            try:
                await product.product.identity.require_current(ProductGrant(
                    admitted.actor_id, admitted.authority_version,
                ))
            except HTTPException as error:
                if error.status_code >= 500:
                    raise
                status = ""
        return Target(thread.channel_id, thread.thread_ts, status, version)

    async def claim(self, thread_id: str) -> str | None:
        try:
            async with self.runtime.database.sessions.begin() as db:
                if await db.get(SlackActivity, thread_id) is None:
                    db.add(SlackActivity(thread_id=thread_id))
        except IntegrityError:
            pass
        stamp, identity = time.time(), new_id()
        async with self.runtime.database.sessions.begin() as db:
            return await db.scalar(update(SlackActivity).where(
                SlackActivity.thread_id == thread_id, SlackActivity.claim_until <= stamp,
            ).values(
                claim_id=identity, claim_until=stamp + CLAIM_SECONDS, checked_at=stamp,
            ).returning(SlackActivity.claim_id))

    async def write(self, thread_id: str, claim: str, **values) -> None:
        async with self.runtime.database.sessions.begin() as db:
            await db.execute(update(SlackActivity).where(
                SlackActivity.thread_id == thread_id, SlackActivity.claim_id == claim,
            ).values(**values))

    async def target_or_retire(self, thread_id: str, claim: str) -> Target | None:
        target = await self.target(thread_id)
        if target is None:
            # No authorized destination remains for a cleanup request. Slack's
            # TTL clears its display; idle rows must leave the dirty queue.
            # Active turns remain candidates and can recover when reconnected.
            await self.write(
                thread_id, claim, status="", retry_at=0, refreshed_at=0, delivery_version="",
            )
        return target

    async def sync_one(self, client: httpx.AsyncClient, thread_id: str) -> None:
        claim = await self.claim(thread_id)
        if claim is None:
            return
        try:
            target = await self.target_or_retire(thread_id, claim)
            if target is None:
                return
            async with self.runtime.database.sessions() as db:
                saved = await db.get(SlackActivity, thread_id)
            assert saved is not None
            stamp = time.time()
            changed = saved.status != target.status
            if not changed:
                if saved.retry_at > stamp:
                    return
                if not target.status and not saved.retry_at:
                    return  # Never backfill historical idle threads.
                if (
                    saved.refreshed_at > stamp - REFRESH_SECONDS
                    and saved.delivery_version == target.delivery_version
                ):
                    return
            product = self.runtime.product_slack
            token = (
                await product.connectors.slack_bot_token() if product
                else self.runtime.settings.slack_bot_token.get_secret_value()
            )
            if not token:
                return
            # Token refresh can suspend long enough for Stop, pause, revocation,
            # completion, or replacement of the connected workspace.
            target = await self.target_or_retire(thread_id, claim)
            if target is None:
                return
            await self.write(
                thread_id, claim, status=target.status, delivery_version=target.delivery_version,
                refreshed_at=0, retry_at=time.time() + 30,
            )
            # Re-read after the durable attempt, immediately before external I/O.
            current = await self.target_or_retire(thread_id, claim)
            if current != target:
                await self.write(thread_id, claim, retry_at=0)
                return
            response = await client.post(
                "assistant.threads.setStatus",
                headers={"Authorization": "Bearer " + token},
                json={"channel_id": target.channel, "thread_ts": target.thread_ts, "status": target.status},
                timeout=REQUEST_SECONDS,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Slack did not confirm the working status.")
            await self.write(thread_id, claim, refreshed_at=time.time(), retry_at=0)
        finally:
            await self.write(thread_id, claim, claim_id="", claim_until=0)

    async def sync(self, client: httpx.AsyncClient | None = None) -> None:
        runtime = self.runtime
        if runtime.product_slack is None and not runtime.settings.slack_bot_token.get_secret_value():
            return
        async with runtime.database.sessions() as db:
            working = select(Turn.id).where(
                Turn.session_id == SlackThread.session_id, Turn.status.in_(STATUSES),
                ~Turn.workflow_id.startswith("moyai-slack-control-"),
            ).exists()
            threads = list(await db.scalars(select(SlackThread.id).outerjoin(
                SlackActivity, SlackActivity.thread_id == SlackThread.id,
            ).where(or_(
                working, SlackActivity.status != "", SlackActivity.retry_at > 0,
            )).order_by(func.coalesce(SlackActivity.checked_at, 0), SlackThread.id).limit(25)))
        semaphore = asyncio.Semaphore(5)

        async def send(transport: httpx.AsyncClient, thread_id: str) -> None:
            async with semaphore:
                try:
                    # Includes token refresh and database waits, not just HTTP.
                    async with asyncio.timeout(REQUEST_SECONDS):
                        await self.sync_one(transport, thread_id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - status failure must not stop answer delivery
                    logger.warning("Slack working status will reconcile (%s).", type(error).__name__)

        if client is None:
            async with httpx.AsyncClient(base_url="https://slack.com/api/") as own:
                await asyncio.gather(*(send(own, identity) for identity in threads))
        else:
            await asyncio.gather(*(send(client, identity) for identity in threads))

    async def watch(self) -> None:
        while True:
            self.wake.clear()
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - recover transient store/provider failures
                logger.warning("Slack working status is unavailable (%s).", type(error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=3)
            except TimeoutError:
                pass
