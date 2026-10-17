"""Exact cost-receipt lookup for admitted native requests; no inference replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError

from .inference_usage import InferenceUsage, money
from .models import now
from .product_inference import ProductInferenceRequest

if TYPE_CHECKING:
    from .product_inference import ProductInferenceService

log = logging.getLogger(__name__)


class ProductInferenceRecovery:
    def __init__(self, service: ProductInferenceService):
        self.service = service
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.watch())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def matches(self, row: ProductInferenceRequest) -> bool:
        return bool(
            self.service.settings.litellm_spend_recovery_enabled
            and row.key_hash and row.gateway_scope
            and (row.gateway_scope, row.key_hash) == self.service.scope()
        )

    async def _orphan(self, row: ProductInferenceRequest) -> None:
        if row.status != "dispatching":
            return
        async with self.service.database.sessions.begin() as db:
            live = await db.scalar(text(
                "SELECT 1 FROM pg_stat_activity WHERE pid=:pid AND backend_start=CAST(:started AS timestamptz)"
            ), {"pid": row.owner_pid, "started": datetime.fromisoformat(row.owner_started)})
            if live:
                return
            current = await db.get(ProductInferenceRequest, row.id, with_for_update=True)
            if current is not None and current.status == "dispatching":
                current.status = "unknown"
                current.finished_at = now()
                usage = await db.get(InferenceUsage, current.request_key)
                if usage is not None and usage.status == "pending":
                    usage.status, usage.finished_at = "interrupted", current.finished_at
                    usage.coverage_error = "request_owner_lost"

    async def recover_orphans(self) -> int:
        """No TTL reclaim: exact owner backend absence proves local work died."""
        from app.spend_recovery import BATCH_SIZE

        async with self.service.database.sessions() as db:
            identifiers = (await db.execute(text("""
                SELECT r.id FROM moyai_product_inference_requests r
                WHERE r.status='dispatching' AND NOT EXISTS (
                    SELECT 1 FROM pg_stat_activity a
                    WHERE a.pid=r.owner_pid AND a.backend_start=CAST(r.owner_started AS timestamptz)
                ) ORDER BY r.created_at,r.id LIMIT :limit
            """), {"limit": BATCH_SIZE})).scalars().all()
            rows = list((await db.scalars(select(ProductInferenceRequest).where(
                ProductInferenceRequest.id.in_(identifiers),
            ))).all()) if identifiers else []
        for row in rows:
            await self._orphan(row)  # Recheck in a fresh transaction/snapshot.
        return len(rows)

    async def defer(self, row: ProductInferenceRequest, reason: str) -> None:
        async with self.service.database.sessions.begin() as db:
            current = await db.get(ProductInferenceRequest, row.id, with_for_update=True)
            if current is None or current.cost_usd is not None or not self.matches(current):
                return
            current.recovery_attempts += 1
            current.recovery_error = reason
            # Permissions can be repaired without changing the request's key
            # scope. Keep a bounded retry instead of permanently abandoning
            # a paid request after the first 401/403.
            retryable = reason in {"not_found", "gateway_unavailable", "receipt_access_denied"}
            current.next_attempt_at = (
                datetime.now(UTC) + timedelta(seconds=min(3600, 60 * 2 ** min(current.recovery_attempts - 1, 6)))
            ).isoformat() if retryable else None

    async def settle(self, row: ProductInferenceRequest, receipt_id: str, cost: str) -> bool:
        if money(cost) is None:
            return False
        async with self.service.database.sessions.begin() as db:
            current = await db.get(ProductInferenceRequest, row.id, with_for_update=True)
            if (
                current is None or current.cost_usd is not None or not self.matches(current)
                or (current.gateway_scope, current.key_hash) != (row.gateway_scope, row.key_hash)
                or current.status == "not_dispatched"
            ):
                return False
            usage = await db.get(InferenceUsage, current.request_key, with_for_update=True)
            if usage is None:
                return False
            receipt_key = hashlib.sha256(
                f"{current.gateway_scope}/{current.key_hash}/{receipt_id}".encode()
            ).hexdigest()
            current.receipt_key = usage.receipt_key = receipt_key
            current.cost_usd = usage.cost_usd = cost
            current.cost_source = usage.cost_source = "gateway_recovery"
            # The original ID is already incorporated into the scoped unique
            # receipt_key above. Preserve a stable reference within the
            # existing varchar(200) column for long Responses gateway IDs.
            current.cost_receipt_id = (
                receipt_id if len(receipt_id) <= 200
                else "sha256:" + hashlib.sha256(receipt_id.encode()).hexdigest()
            )
            current.recovery_error, current.next_attempt_at = None, None
            usage.cost_status = "actual"
            if current.status == "dispatching":
                # A spend receipt proves the charge, not delivery of the SDK
                # response. Never claim that missing response acknowledgement.
                current.status, current.finished_at = "unknown", now()
                usage.status, usage.finished_at = "interrupted", current.finished_at
                usage.coverage_error = usage.coverage_error or "response_outcome_unknown"
            return True

    async def process(self, client: httpx.AsyncClient, row: ProductInferenceRequest) -> None:
        from app.spend_recovery import MAX_RECEIPT_BYTES, REQUEST_TIMEOUT, ReceiptError, receipt

        if row.cost_usd is not None or row.status == "not_dispatched" or not self.matches(row):
            return
        settings = self.service.settings
        base, key = settings.litellm_api_base.rstrip("/").removesuffix("/v1"), settings.litellm_api_key
        try:
            created = datetime.fromisoformat(row.created_at)
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with client.stream(
                    "GET", base + "/spend/logs/v2",
                    params={
                        "request_id": row.id, "api_key": row.key_hash,
                        "start_date": (created.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
                        "end_date": (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
                        "page": 1, "page_size": 2,
                    },
                    headers={"Authorization": "Bearer " + key}, follow_redirects=False,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ReceiptError("receipt_access_denied")
                    if response.status_code != 200:
                        raise ReceiptError("gateway_unavailable")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RECEIPT_BYTES:
                            raise ReceiptError("oversized_receipt")
                        data.extend(chunk)
            identifier, cost = receipt(json.loads(data, parse_float=Decimal), {
                "id": row.id, "gateway_id": row.gateway_id or "", "key_hash": row.key_hash,
            })
            if money(cost) is None:
                raise ReceiptError("invalid_receipt")
            # Reuse the exact legacy receipt validator, including rejection of
            # unverified log zero and ambiguous/conflicting request identifiers.
            if not self.matches(row) or settings.litellm_api_key != key:
                return
            await self.settle(row, identifier, cost)
        except asyncio.CancelledError:
            raise
        except ReceiptError as error:
            await self.defer(row, str(error))
        except IntegrityError:
            await self.defer(row, "receipt_already_used")
        except (httpx.HTTPError, TimeoutError):
            await self.defer(row, "gateway_unavailable")
        except (ValueError, TypeError, RecursionError):
            await self.defer(row, "invalid_receipt")

    async def poll(self, client: httpx.AsyncClient | None = None) -> int:
        from app.spend_recovery import BATCH_SIZE, REQUEST_TIMEOUT

        orphaned = await self.recover_orphans()
        if not self.service.settings.litellm_spend_recovery_enabled:
            return orphaned
        gateway, key_hash = self.service.scope()
        async with self.service.database.sessions() as db:
            rows = list((await db.scalars(select(ProductInferenceRequest).where(
                ProductInferenceRequest.cost_usd.is_(None),
                ProductInferenceRequest.status != "not_dispatched",
                ProductInferenceRequest.gateway_scope == gateway,
                ProductInferenceRequest.key_hash == key_hash,
                or_(
                    ProductInferenceRequest.next_attempt_at <= now(),
                    # Earlier versions parked access-denied receipts forever.
                    # Re-admit only that repairable state; ambiguous or invalid
                    # charge evidence still requires explicit investigation.
                    and_(
                        ProductInferenceRequest.next_attempt_at.is_(None),
                        ProductInferenceRequest.recovery_error == "receipt_access_denied",
                    ),
                ),
            ).order_by(ProductInferenceRequest.next_attempt_at, ProductInferenceRequest.id).limit(BATCH_SIZE))).all())
        if client is None:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=httpx.Limits(max_connections=1)) as owned:
                for row in rows:
                    await self.process(owned, row)
        else:
            for row in rows:
                await self.process(client, row)
        return len(rows)

    async def watch(self) -> None:
        from app.spend_recovery import POLL_SECONDS

        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - failed lookup must never replay inference
                # Provider/DB errors may include secrets; log no exception body.
                log.warning("Native cost receipt recovery paused; admitted requests remain recorded.")
            await asyncio.sleep(POLL_SECONDS)
