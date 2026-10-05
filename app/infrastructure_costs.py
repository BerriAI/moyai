"""Durable infrastructure ledger, monthly bill reconciliation, and billing sync jobs."""
import asyncio
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import billing_sources
from .db import now
from .security import digest
from .spend import period

log = logging.getLogger(__name__)
PROVIDERS = {'render': 'Render', 'modal': 'Modal', 'temporal': 'Temporal Cloud'}


class MonthlyBill(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    provider: str = Field(min_length=1, max_length=80)
    month: str = Field(pattern=r'^\d{4}-\d{2}$')
    amount: Decimal = Field(ge=-1000000000, le=1000000000, max_digits=16, decimal_places=6)
    kind: Literal['invoice', 'estimate'] = 'invoice'
    note: str = Field(default='', max_length=500)
    revision: int = Field(default=0, ge=0)

    @field_validator('provider')
    @classmethod
    def provider_name(cls, value):
        value = value.lower()
        if value == 'temporal cloud':
            value = 'temporal'
        if not re.fullmatch(r'[a-z0-9][a-z0-9 ._-]*', value):
            raise ValueError('Use a provider name with letters, numbers, spaces, dots or dashes.')
        return value

    @field_validator('month')
    @classmethod
    def valid_month(cls, value):
        date.fromisoformat(value + '-01')
        return value


class SyncRange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: date
    end: date


class InfrastructureCosts:
    def __init__(self, store, settings, security, checkpoints):
        self.store, self.settings, self.security, self.checkpoints = store, settings, security, checkpoints
        self.task = None
        self.wake = asyncio.Event()
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS infrastructure_bills (
                    provider TEXT NOT NULL, month TEXT NOT NULL, amount TEXT NOT NULL,
                    kind TEXT NOT NULL, note TEXT NOT NULL, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(provider,month)
                );
                CREATE TABLE IF NOT EXISTS infrastructure_bill_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, month TEXT NOT NULL,
                    actor TEXT NOT NULL, changed_at TEXT NOT NULL, previous TEXT NOT NULL, replacement TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS infrastructure_daily_costs (
                    provider TEXT NOT NULL, scope TEXT NOT NULL, day TEXT NOT NULL, amount TEXT NOT NULL,
                    synced_at TEXT NOT NULL, PRIMARY KEY(provider,scope,day)
                );
                CREATE TABLE IF NOT EXISTS infrastructure_sync_jobs (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, scope TEXT NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, status TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    UNIQUE(provider,scope,start,end)
                );
            ''')

    def scope(self, provider):
        if provider == 'modal':
            value = [self.settings.modal_app_name, sorted(self.settings.modal_billing_object_ids.split(','))]
        else:
            value = [self.settings.temporal_namespace]
        return digest(json.dumps(value))

    def enabled(self, provider):
        if provider == 'modal':
            return bool(self.settings.modal_billing_enabled and self.settings.modal_token_id and self.settings.modal_token_secret)
        return bool(self.settings.temporal_billing_api_key and self.settings.temporal_namespace != 'default')

    def report(self, start, end):
        bills = self.store.rows('SELECT * FROM infrastructure_bills WHERE month>=? AND month<=? ORDER BY month DESC,provider',
                               (start.strftime('%Y-%m'), end.strftime('%Y-%m')))
        bill_map = {(r['provider'], r['month']): r for r in bills}
        names = set(PROVIDERS) | {r['provider'] for r in bills}
        days = [start + timedelta(days=i) for i in range((end-start).days+1)]
        providers, total, estimated = [], Decimal(0), Decimal(0)
        for provider in sorted(names):
            rows = self.store.rows('SELECT * FROM infrastructure_daily_costs WHERE provider=? AND scope=? AND day>=? AND day<=?',
                                   (provider, self.scope(provider), start.isoformat(), end.isoformat()))
            synced = {r['day']: r for r in rows}
            spend, estimate, missing, covered = Decimal(0), Decimal(0), 0, 0
            for day in days:
                bill = bill_map.get((provider, day.strftime('%Y-%m')))
                if bill:
                    covered += 1
                elif day.isoformat() in synced:
                    spend += Decimal(synced[day.isoformat()]['amount'])
                    covered += 1
                else:
                    missing += 1
            # Allocate monthly bills once per overlap, preserving the exact full-month amount.
            for bill in (b for b in bills if b['provider'] == provider):
                month = date.fromisoformat(bill['month'] + '-01')
                length = calendar.monthrange(month.year, month.month)[1]
                overlap = (min(end, month.replace(day=length)) - max(start, month)).days + 1
                allocated = Decimal(bill['amount']) * overlap / length
                spend += allocated
                if bill['kind'] == 'estimate':
                    estimate += allocated
            jobs = self.store.rows('''SELECT status,error,updated_at,start,end FROM infrastructure_sync_jobs
                WHERE provider=? AND scope=? AND start<=? AND end>=? ORDER BY (status='pending') DESC,updated_at DESC LIMIT 1''',
                (provider, self.scope(provider), end.isoformat(), start.isoformat()))
            providers.append({'provider': provider, 'name': PROVIDERS.get(provider, provider), 'spend': str(spend),
                              'estimated': str(estimate), 'missing_days': missing, 'covered_days': covered,
                              'sync_enabled': self.enabled(provider) if provider in {'modal','temporal'} else False,
                              'last_synced_at': max((r['synced_at'] for r in rows), default=None),
                              'sync': jobs[0] if jobs else None})
            total += spend
            estimated += estimate
        return {'spend': str(total), 'estimated': str(estimated), 'providers': providers,
                'bills': [{k: v for k, v in b.items() if k != 'actor'} for b in bills],
                'incomplete': any(p['missing_days'] for p in providers),
                'pending': any(p['sync'] and p['sync']['status'] == 'pending' for p in providers)}

    def save_bill(self, bill, actor):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM infrastructure_bills WHERE provider=? AND month=?', (bill.provider, bill.month)).fetchone()
            if (old['revision'] if old else 0) != bill.revision:
                raise HTTPException(409, 'This bill already exists or changed. Refresh and edit the saved bill.')
            conn.execute('INSERT INTO infrastructure_bill_audit(provider,month,actor,changed_at,previous,replacement) VALUES(?,?,?,?,?,?)',
                         (bill.provider, bill.month, actor, now(), json.dumps(dict(old)) if old else '', bill.model_dump_json()))
            conn.execute('''INSERT INTO infrastructure_bills VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,month) DO UPDATE SET amount=excluded.amount,kind=excluded.kind,note=excluded.note,
                revision=excluded.revision,updated_at=excluded.updated_at,actor=excluded.actor''',
                (bill.provider, bill.month, str(bill.amount), bill.kind, bill.note, bill.revision+1, now(), actor))

    def delete_bill(self, provider, month, revision, actor):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM infrastructure_bills WHERE provider=? AND month=?', (provider, month)).fetchone()
            if not old:
                raise HTTPException(404, 'Bill not found.')
            if old['revision'] != revision:
                raise HTTPException(409, 'This bill changed. Refresh before removing it.')
            conn.execute('INSERT INTO infrastructure_bill_audit(provider,month,actor,changed_at,previous,replacement) VALUES(?,?,?,?,?,?)',
                         (provider, month, actor, now(), json.dumps(dict(old)), ''))
            conn.execute('DELETE FROM infrastructure_bills WHERE provider=? AND month=?', (provider, month))

    def enqueue(self, start, end):
        period(start, end)
        today = datetime.now(timezone.utc).date()
        for provider in ('modal', 'temporal'):
            if not self.enabled(provider):
                continue
            # Modal excludes partial days; Temporal's export lags at least 24 hours.
            upper = min(end, today - timedelta(days=1 if provider == 'modal' else 2))
            if start > upper:
                continue
            self.store.execute('''INSERT INTO infrastructure_sync_jobs(id,provider,scope,start,end,status,state,updated_at)
                VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(provider,scope,start,end) DO UPDATE SET
                status='pending',error='',state=CASE WHEN status='pending' THEN state ELSE excluded.state END,updated_at=CASE WHEN status='pending' THEN updated_at ELSE excluded.updated_at END''',
                (str(uuid4()), provider, self.scope(provider), str(start), str(upper), json.dumps({'operation_id': str(uuid4())}), now()))
        self.wake.set()

    async def process(self, job):
        if job['scope'] != self.scope(job['provider']) or not self.enabled(job['provider']):
            self.store.execute("UPDATE infrastructure_sync_jobs SET status='error',error='Billing configuration changed.',updated_at=? WHERE id=?", (now(),job['id']))
            return
        start, end = date.fromisoformat(job['start']), date.fromisoformat(job['end'])
        try:
            async with asyncio.timeout(55):
                if job['provider'] == 'modal':
                    values = await billing_sources.modal_costs(self.settings, start, end)
                else:
                    state = json.loads(job['state'])
                    def save_state(value):
                        self.store.execute('UPDATE infrastructure_sync_jobs SET state=? WHERE id=?', (json.dumps(value), job['id']))
                    values = await billing_sources.temporal_costs(self.settings, start, end, state, save_state)
            if values is None:
                # Avoid an indefinitely pending report after an upstream failure.
                if datetime.now(timezone.utc) - datetime.fromisoformat(job['updated_at']) > timedelta(hours=2):
                    raise billing_sources.BillingUnavailable('Billing report timed out. Retry sync.')
                return
            with self.store.connect() as conn:
                timestamp = now()
                for i in range((end-start).days+1):
                    day = str(start + timedelta(days=i))
                    cost = str(billing_sources.amount(values.get(day, '0')))
                    conn.execute('''INSERT INTO infrastructure_daily_costs VALUES(?,?,?,?,?)
                        ON CONFLICT(provider,scope,day) DO UPDATE SET amount=excluded.amount,synced_at=excluded.synced_at''',
                        (job['provider'],job['scope'],day,cost,timestamp))
                conn.execute("UPDATE infrastructure_sync_jobs SET status='complete',error='',updated_at=? WHERE id=?", (timestamp,job['id']))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider exceptions can contain auth headers or signed report URLs.
            message = str(exc) if isinstance(exc, billing_sources.BillingUnavailable) else 'Billing sync failed. Check billing permissions, plan access and date range, then retry.'
            self.store.execute("UPDATE infrastructure_sync_jobs SET status='error',error=?,updated_at=? WHERE id=?", (message,now(),job['id']))
            log.warning('Infrastructure billing sync failed for %s (%s)', job['provider'], type(exc).__name__)
        await self.checkpoints.flush()

    def start(self):
        self.task = asyncio.create_task(self.watch())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def watch(self):
        refresh_at = datetime.min.replace(tzinfo=timezone.utc)
        while True:
            try:
                self.wake.clear()
                current = datetime.now(timezone.utc)
                if current >= refresh_at:
                    first = current.date().replace(day=1)
                    self.enqueue((first-timedelta(days=1)).replace(day=1), current.date())
                    refresh_at = current + timedelta(hours=6)
                    self.wake.clear()
                seen = set()
                for job in self.store.rows("SELECT * FROM infrastructure_sync_jobs WHERE status='pending' ORDER BY updated_at"):
                    if job['provider'] not in seen:
                        seen.add(job['provider'])
                        await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Infrastructure sync paused (%s)', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=60)
            except TimeoutError:
                pass

    def routes(self):
        router = APIRouter()

        @router.put('/api/admin/spend/infrastructure/bills')
        async def save(body: MonthlyBill, request: Request):
            self.security.require(request, mutation=True, admin=True)
            self.save_bill(body, self.store.identity(self.security.session_info(request)))
            await self.checkpoints.flush()
            return {'ok': True}

        @router.delete('/api/admin/spend/infrastructure/bills')
        async def delete(request: Request, provider: str, month: str, revision: int):
            self.security.require(request, mutation=True, admin=True)
            self.delete_bill(provider, month, revision, self.store.identity(self.security.session_info(request)))
            await self.checkpoints.flush()
            return {'ok': True}

        @router.post('/api/admin/spend/infrastructure/sync')
        async def sync(body: SyncRange, request: Request):
            self.security.require(request, mutation=True, admin=True)
            self.enqueue(body.start, body.end)
            await self.checkpoints.flush()
            return {'ok': True}

        return router
