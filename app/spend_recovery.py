"""Recover orphaned costs from exact gateway receipts, outside inference."""
import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import sqlite3
from typing import TYPE_CHECKING

import httpx

from .db import now
from .spend import money

if TYPE_CHECKING:
    from .spend import Spend

log = logging.getLogger(__name__)
BATCH_SIZE = 16
POLL_SECONDS = 5
REQUEST_TIMEOUT = 10
MAX_RECEIPT_BYTES = 256 * 1024


class ReceiptError(ValueError):
    """A fixed, safe reason; upstream exception bodies may contain secrets."""


async def database[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    # Store opens a connection per operation. Keep disk waits off streaming's
    # event loop, and finish an outstanding write before lifecycle shutdown.
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def receipt(value: object, row: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(value, dict) or value.get('total_is_capped') is not False:
        raise ReceiptError('incomplete_receipts')
    total, size, pages = (value.get(key) for key in ('total', 'page_size', 'total_pages'))
    records = value.get('data')
    if (type(total) is not int or type(size) is not int or type(pages) is not int
            or total < 0 or size < 1 or value.get('page') != 1
            or pages != (total + size - 1) // size
            or not isinstance(records, list) or len(records) != total):
        raise ReceiptError('incomplete_receipts')
    if total == 0:
        raise ReceiptError('not_found')
    if total != 1 or not isinstance(records[0], dict):
        raise ReceiptError('ambiguous_receipts')
    item = records[0]
    identifier = item.get('request_id')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 200:
        raise ReceiptError('invalid_receipt')
    if item.get('api_key') != row['key_hash']:
        raise ReceiptError('scope_mismatch')
    metadata = item.get('metadata')
    if metadata is None:
        metadata = {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            raise ReceiptError('invalid_receipt') from None
    if not isinstance(metadata, dict):
        raise ReceiptError('invalid_receipt')
    nested = metadata.get('spend_logs_metadata')
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ReceiptError('invalid_receipt')
    calls = (item.get('litellm_call_id'), metadata.get('litellm_call_id'))
    moyai_ids = (metadata.get('moyai_request_id'), nested.get('moyai_request_id'))
    allowed = {row['id'], row['gateway_id']}
    if (any(value is not None and (not isinstance(value, str) or not value or value not in allowed) for value in calls)
            or any(value is not None and value != row['id'] for value in moyai_ids)
            or row['id'] not in (identifier, *calls, *moyai_ids)):
        raise ReceiptError('conflicting_identifiers')
    cost = money(item.get('spend'))
    if cost is None or item.get('status') not in {'success', 'failure'}:
        raise ReceiptError('invalid_receipt')
    # LiteLLM's spend column defaults to zero when pricing was unavailable.
    # Actual zero costs returned in normal response headers/usage remain valid.
    if Decimal(cost) == 0:
        raise ReceiptError('unverified_zero')
    return identifier, cost


class SpendRecovery:
    def __init__(self, spend: 'Spend') -> None:
        self.spend = spend
        self.task: asyncio.Task[None] | None = None

    def scope_matches(self, row: Mapping[str, object]) -> bool:
        return bool(self.spend.settings.litellm_spend_recovery_enabled
                    and self.spend.key_hash and self.spend.gateway_scope
                    and row['key_hash'] == self.spend.key_hash
                    and row['gateway_scope'] == self.spend.gateway_scope)

    def defer(self, row: Mapping[str, object], reason: str) -> None:
        attempts = int(row['cost_recovery_attempts']) + 1
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 60 * 2 ** min(attempts - 1, 6)))).isoformat()
        self.spend.store.execute('''UPDATE model_requests SET cost_next_attempt_at=?,
            cost_recovery_attempts=cost_recovery_attempts+1,cost_recovery_error=?
            WHERE id=? AND cost IS NULL AND key_hash=? AND gateway_scope=?''',
            (retry_at, reason, row['id'], row['key_hash'], row['gateway_scope']))

    async def process(self, client: httpx.AsyncClient, row: Mapping[str, object]) -> None:
        if row['cost'] is not None or row['status'] == 'pending' or not self.scope_matches(row):
            return
        settings = self.spend.settings
        base, key = settings.litellm_api_base.rstrip('/').removesuffix('/v1'), settings.litellm_api_key
        try:
            # v2 requires a UTC date window even for exact request-ID lookups.
            created = datetime.fromisoformat(str(row['created_at']).replace('Z', '+00:00'))
            start = (created.astimezone(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
            end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with client.stream('GET', base + '/spend/logs/v2',
                    params={'request_id': str(row['id']), 'api_key': str(row['key_hash']),
                            'start_date': start, 'end_date': end, 'page': 1, 'page_size': 2},
                    headers={'Authorization': 'Bearer ' + key}, follow_redirects=False) as response:
                    if response.status_code in (401, 403):
                        raise ReceiptError('receipt_access_denied')
                    if response.status_code != 200:
                        raise ReceiptError('gateway_unavailable')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RECEIPT_BYTES:
                            raise ReceiptError('oversized_receipt')
                        data.extend(chunk)
            identifier, cost = receipt(json.loads(data, parse_float=Decimal), row)
            # Settings or response accounting may have changed while awaiting I/O.
            if not self.scope_matches(row) or settings.litellm_api_key != key:
                return
            await database(self.spend.settle_receipt, str(row['id']), str(row['key_hash']), str(row['gateway_scope']), identifier, cost)
        except asyncio.CancelledError:
            raise
        except ReceiptError as exc:
            await database(self.defer, row, str(exc))
        except sqlite3.IntegrityError:
            await database(self.defer, row, 'receipt_already_used')
        except (httpx.HTTPError, TimeoutError):
            await database(self.defer, row, 'gateway_unavailable')
        except (ValueError, TypeError, RecursionError):
            await database(self.defer, row, 'invalid_receipt')

    async def poll(self, client: httpx.AsyncClient) -> None:
        if (not self.spend.settings.litellm_spend_recovery_enabled
                or not self.spend.key_hash or not self.spend.gateway_scope):
            return
        rows = await database(self.spend.store.rows, '''SELECT * FROM model_requests
            WHERE cost IS NULL AND status!='pending' AND key_hash=? AND gateway_scope=?
                AND cost_next_attempt_at!='' AND cost_next_attempt_at<=?
            ORDER BY cost_next_attempt_at,id LIMIT ?''',
            (self.spend.key_hash, self.spend.gateway_scope, now(), BATCH_SIZE))
        for row in rows:
            await self.process(client, row)
            await asyncio.sleep(0.05)
        if rows:
            await self.spend.checkpoints.flush()

    def start(self) -> None:
        if self.task is None and self.spend.settings.litellm_spend_recovery_enabled:
            self.task = asyncio.create_task(self.watch())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def watch(self) -> None:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=httpx.Limits(max_connections=1)) as client:
            while True:
                try:
                    await self.poll(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning('Cost receipt recovery paused (%s)', type(exc).__name__)
                await asyncio.sleep(POLL_SECONDS)
