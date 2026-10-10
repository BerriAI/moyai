"""Measure the real PostgreSQL wake pump and admission loop without provider calls.

Set MOYAI_TEST_POSTGRES_URL to a disposable database. Only a random probe schema
is created/dropped. Temporal RPC latency is simulated; this is not a provider or
Temporal throughput benchmark. Admission contention uses an actual writer lock.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.db import database
from app.temporal_runtime import TemporalRunManager
from scripts.runtime_capacity_probe import open_store


async def wake_probe(manager, sessions):
    delivered = []
    started = time.monotonic()

    async def start_workflow(*args, **kwargs):
        await asyncio.sleep(.005)
        delivered.append((time.monotonic() - started) * 1000)

    async def connect():
        return SimpleNamespace(start_workflow=start_workflow)

    manager.connect_temporal = connect
    serving = asyncio.create_task(manager.serve())
    try:
        async with asyncio.timeout(120):
            while len(delivered) < sessions:
                if serving.done():
                    await serving
                    raise RuntimeError('Wake pump stopped before all deliveries')
                await asyncio.sleep(.01)
            while (await database(manager.store.rows,
                    'SELECT 1 FROM durable_sessions WHERE revision>delivered LIMIT 1')):
                await asyncio.sleep(.01)
        return {'sessions': sessions, 'batch_size': manager.settings.temporal_dispatch_batch_size,
                'concurrency': manager.settings.temporal_dispatch_concurrency,
                'rpc_simulated_ms': 5, 'first_wake_ms': round(delivered[0], 2),
                'p50_ms': round(delivered[(sessions - 1) // 2], 2),
                'p95_ms': round(delivered[int((sessions - 1) * .95)], 2),
                'last_wake_ms': round(delivered[-1], 2)}
    finally:
        manager.closing = True
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


async def admission_probe(manager, run_id, hold_seconds=.7):
    locked = threading.Event()
    delays = []
    stop = False

    def hold_writer():
        with manager.store.connect() as conn:
            conn.begin_write()
            locked.set()
            time.sleep(hold_seconds)

    async def tick():
        while not stop:
            before = time.monotonic()
            await asyncio.sleep(.01)
            delays.append(max(0, time.monotonic() - before - .01) * 1000)

    async def synthetic_step(run_id, state):
        return True

    manager.step = synthetic_step
    holder = asyncio.create_task(asyncio.to_thread(hold_writer))
    pulse = asyncio.create_task(tick())
    try:
        await asyncio.to_thread(locked.wait, 5)
        assert locked.is_set()
        started = time.monotonic()
        assert await manager.advance(run_id) is True
        await holder
        await asyncio.sleep(.02)
        return {'writer_hold_ms': hold_seconds * 1000,
                'admission_ms': round((time.monotonic() - started) * 1000, 2),
                'max_event_loop_lag_ms': round(max(delays), 2)}
    finally:
        stop = True
        await asyncio.gather(holder, pulse)


async def measure(sessions):
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        raise SystemExit('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_startup_' + uuid4().hex
    store = None
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-startup-') as directory:
            settings = Settings(_env_file=None, data_dir=Path(directory), moyai_database_url=url,
                moyai_database_schema=schema, moyai_database_initialize=True, temporal_enabled=True,
                moyai_runtime_role='coordinator', object_storage_bucket='synthetic-only',
                session_secret=uuid4().hex, encryption_key=Fernet.generate_key().decode(),
                max_pending_runs=max(100, sessions + 1))
            store = open_store(settings)
            manager = TemporalRunManager(store, settings)
            for _ in range(sessions):
                run = store.create_run('Synthetic startup probe', '', 'demo', [], chat_enabled=True)
                manager.submit(run)
            wake = await wake_probe(manager, sessions)
            admission = await admission_probe(manager, run['id'])
            return {'backend': 'postgresql', 'no_provider_calls': True,
                    'wake_delivery': wake, 'contended_admission': admission}
    finally:
        if store:
            store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(schema)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=int, default=3000)
    args = parser.parse_args()
    if not 1 <= args.sessions <= 10000:
        parser.error('Use 1–10000 sessions.')
    print(json.dumps(asyncio.run(measure(args.sessions)), indent=2))
