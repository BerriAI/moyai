"""Monthly provider statements, separate from the per-request model ledger.

One replaceable statement per provider/month avoids adding a refreshed month-to-
date total twice. Original revisions remain in the audit log. No provider calls
or recurring cost assumptions are made by the reporting endpoint.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

import modal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .db import now

Provider = Literal['modal', 'temporal', 'render']
PROVIDERS = ('modal', 'temporal', 'render')
Amount = Annotated[Decimal, Field(ge=0, le=10_000_000, max_digits=20, decimal_places=12)]


def month_bounds(value: str):
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', value):
        raise ValueError('Choose a calendar month in YYYY-MM format.')
    start = datetime.fromisoformat(value + '-01T00:00:00+00:00')
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


class Statement(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    month: str = Field(max_length=7)
    usage_cost: Amount
    allocated_fee: Amount
    # None means unknown. Zero must be explicitly confirmed by an admin.
    credits_applied: Amount | None = None
    scope: str = Field(min_length=3, max_length=500)
    source_reference: str = Field(min_length=3, max_length=500)
    allocation_note: str = Field(default='', max_length=1000)
    observed_at: datetime
    finalized: bool = False
    revision: int = Field(default=0, ge=0)

    @field_validator('month')
    @classmethod
    def valid_month(cls, value):
        month_bounds(value)
        return value

    @field_validator('observed_at')
    @classmethod
    def valid_time(cls, value):
        if value.tzinfo is None:
            raise ValueError('Include a timezone for the billing observation.')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def consistent(self):
        start, end = month_bounds(self.month)
        if self.observed_at < start or self.observed_at > datetime.now(timezone.utc):
            raise ValueError('Observation must be after the month began and not in the future.')
        if self.finalized and self.observed_at < end:
            raise ValueError('A statement cannot be final before its billing month ends.')
        if self.credits_applied is not None and self.credits_applied > self.usage_cost + self.allocated_fee:
            raise ValueError('Applied credits cannot exceed the attributed charges.')
        if self.allocated_fee and len(self.allocation_note) < 3:
            raise ValueError('Explain how the shared subscription or support fee was allocated.')
        if self.finalized and self.credits_applied is None:
            raise ValueError('Confirm applied credits, including zero, before marking a statement final.')
        return self


class ModalPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    month: str = Field(max_length=7)


def modal_total(rows, object_ids, start, end):
    """Never attribute a whole shared Modal workspace to Moyai."""
    costs, matched = [], set()
    for row in rows:
        if row.object_id not in object_ids:
            continue
        if not start <= row.interval_start < end:
            raise ValueError('Modal returned a row outside the requested period.')
        value = Decimal(str(row.cost))
        if not value.is_finite() or value < 0:
            raise ValueError('Modal returned an unsupported cost adjustment; reconcile the statement manually.')
        costs.append(value)
        matched.add(row.object_id)
    return sum(costs, Decimal(0)), sorted(matched)


class OperatingCosts:
    def __init__(self, store, settings, security, checkpoints):
        self.store, self.settings, self.security, self.checkpoints = store, settings, security, checkpoints
        self.modal_lock = asyncio.Lock()
        if settings.operating_costs_enabled:
            self.initialize()

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS operating_statements (
                    provider TEXT NOT NULL, month TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, month)
                );
                CREATE TABLE IF NOT EXISTS operating_cost_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                    month TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    actor_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(provider, month, revision)
                );
            ''')

    def require_enabled(self):
        if not self.settings.operating_costs_enabled:
            raise HTTPException(404, 'Infrastructure cost reporting is not enabled.')

    def save(self, provider, body, actor):
        payload = json.dumps(body.model_dump(mode='json', exclude={'revision'}), sort_keys=True)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM operating_statements WHERE provider=? AND month=?', (provider, body.month)).fetchone()
            # A network retry of a completed save is a no-op, including its audit.
            if old and old['payload'] == payload:
                return old['revision']
            revision = old['revision'] if old else 0
            if body.revision != revision:
                raise HTTPException(409, 'This statement changed. Refresh and review the latest revision before saving.')
            revision += 1
            updated = now()
            conn.execute('INSERT OR REPLACE INTO operating_statements VALUES(?,?,?,?,?)',
                         (provider, body.month, payload, revision, updated))
            conn.execute('INSERT INTO operating_cost_audit(provider,month,revision,payload,actor_id,created_at) VALUES(?,?,?,?,?,?)',
                         (provider, body.month, revision, payload, actor, updated))
        return revision

    def report(self, month):
        start, end = month_bounds(month)
        statements = {r['provider']: r for r in self.store.rows('SELECT * FROM operating_statements WHERE month=?', (month,))}
        providers = []
        gross, credits = Decimal(0), Decimal(0)
        credits_known = True
        for provider in PROVIDERS:
            row = statements.get(provider)
            if not row:
                providers.append({'provider': provider, 'statement': None})
                credits_known = False
                continue
            statement = json.loads(row['payload'])
            charge = Decimal(statement['usage_cost']) + Decimal(statement['allocated_fee'])
            credit = statement['credits_applied']
            gross += charge
            credits += Decimal(credit or '0')
            credits_known &= credit is not None
            observed = datetime.fromisoformat(statement['observed_at'].replace('Z', '+00:00'))
            providers.append({'provider': provider, 'statement': {**statement, 'revision': row['revision'],
                'updated_at': row['updated_at'], 'gross': str(charge),
                'net': str(charge - Decimal(credit)) if credit is not None else None,
                'stale': not statement['finalized'] and (datetime.now(timezone.utc) - observed).total_seconds() > 86400}})
        # This overview intentionally covers ALL recorded keys. The existing
        # per-user Spend report still describes the currently configured key.
        requests = self.store.rows('SELECT cost,status FROM model_requests WHERE created_at>=? AND created_at<?', (start.isoformat(), end.isoformat()))
        llm = sum((Decimal(r['cost']) for r in requests if r['cost'] is not None), Decimal(0))
        missing = sum(r['cost'] is None for r in requests)
        tracked = self.store.rows('SELECT MIN(created_at) AS first FROM model_requests')[0]['first']
        return {'enabled': True, 'month': month, 'currency': 'USD', 'timezone': 'UTC',
            'generated_at': now(), 'providers': providers,
            'llm': {'recorded_cost': str(llm), 'requests': len(requests), 'missing_costs': missing, 'tracked_since': tracked},
            'known_infrastructure_cost': str(gross), 'known_combined_cost': str(gross + llm),
            'confirmed_credits': str(credits),
            'known_combined_after_credits': str(gross + llm - credits) if credits_known else None,
            'reported_providers': len(statements), 'all_statements_final': len(statements) == 3 and all(p['statement']['finalized'] for p in providers),
            'modal_import_ready': bool(self.settings.operating_costs_modal_object_ids.strip())}

    async def preview_modal(self, month):
        start, end = month_bounds(month)
        # Modal excludes partial final intervals. Expose the boundary to admins.
        end = min(end, datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0))
        if end <= start:
            raise HTTPException(422, 'No completed billing hours in that month yet.')
        ids = {x.strip() for x in self.settings.operating_costs_modal_object_ids.split(',') if x.strip()}
        if not ids:
            raise HTTPException(422, 'Configure the exact Moyai Modal app and storage object IDs before importing.')
        if not self.settings.modal_token_id or not self.settings.modal_token_secret:
            raise HTTPException(503, 'Modal credentials are not configured.')
        if self.modal_lock.locked():
            raise HTTPException(409, 'A Modal report is already being fetched.')
        async with self.modal_lock:
            try:
                async with asyncio.timeout(45):
                    client = await modal.Client.from_credentials.aio(self.settings.modal_token_id, self.settings.modal_token_secret)
                    workspace = modal.Workspace.from_context(client=client)
                    rows = await workspace.billing.report.aio(start=start, end=end, resolution='h')
                    cost, matched = modal_total(rows, ids, start, end)
            except Exception as exc:
                # Vendor errors may carry authentication details. Never return them.
                raise HTTPException(503, 'Modal billing export is unavailable. Check Team/Enterprise billing access, or enter a provider statement.') from exc
        return {'usage_cost': str(cost), 'through': end.isoformat(), 'observed_at': now(),
                'scope': ', '.join(sorted(ids)), 'matched_object_ids': matched,
                'source_reference': 'Modal Workspace.billing.report, hourly, through ' + end.isoformat(),
                'note': 'Provisional usage only. Review storage coverage, subscription allocation, discounts, egress allowance and credits before saving. No matched rows does not establish zero usage.'}

    def routes(self):
        router = APIRouter()

        @router.get('/api/admin/operating-costs')
        async def report(request: Request, month: str | None = None):
            self.security.require(request, admin=True)
            if not self.settings.operating_costs_enabled:
                return {'enabled': False}
            try:
                return self.report(month or datetime.now(timezone.utc).strftime('%Y-%m'))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

        @router.put('/api/admin/operating-costs/{provider}')
        async def save(provider: Provider, body: Statement, request: Request):
            self.security.require(request, admin=True, mutation=True)
            self.require_enabled()
            actor = self.store.identity(self.security.session_info(request))
            revision = self.save(provider, body, actor)
            await self.checkpoints.flush()
            return {'revision': revision}

        @router.get('/api/admin/operating-costs/audit/{provider}')
        async def audit(provider: Provider, request: Request, month: str):
            self.security.require(request, admin=True)
            self.require_enabled()
            try:
                month_bounds(month)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            rows = self.store.rows('SELECT * FROM operating_cost_audit WHERE provider=? AND month=? ORDER BY revision DESC', (provider, month))
            return [{**r, 'payload': json.loads(r['payload'])} for r in rows]

        @router.post('/api/admin/operating-costs/modal/preview')
        async def preview(body: ModalPreview, request: Request):
            self.security.require(request, admin=True, mutation=True)
            self.require_enabled()
            try:
                return await self.preview_modal(body.month)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None

        return router
