"""Real PostgreSQL admission across worker processes; synthetic sandbox/model work.

Run with MOYAI_TEST_POSTGRES_URL pointing to a disposable database. Creates and
drops only its own random schema. Never contacts a model or sandbox provider.
"""
import argparse
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.db import Store
from app.durable_runner import DurableRunner
from app.model_slots import ModelSlots


def open_store(settings):
    store = Store(settings.data_dir, database_url=settings.moyai_database_url,
        database_schema=settings.moyai_database_schema, database_initialize=True,
        application_instance=True, runtime_role=settings.moyai_runtime_role,
        database_pool_size=settings.moyai_database_pool_size, max_pending_runs=settings.max_pending_runs)
    store.database.configure_runtime(settings)
    return store


def worker(arguments):
    options, ids = arguments
    settings = Settings(_env_file=None, **options)
    store = open_store(settings)

    async def run():
        manager = DurableRunner(store, settings)
        slots = asyncio.Semaphore(12)

        async def synthetic_step(run_id, state):
            # Leave the real admission reservation occupied; no provider call.
            return True

        manager.step = synthetic_step

        async def advance(run_id):
            async with slots:
                return await manager.advance(run_id)

        results = await asyncio.gather(*(advance(run_id) for run_id in ids))
        return {'pid': os.getpid(), 'admitted': results.count(True), 'queued': results.count('capacity')}

    try:
        return asyncio.run(run())
    finally:
        store.close()


async def model_probe(requests, capacity):
    slots = ModelSlots(capacity)
    release = asyncio.Event()
    full = asyncio.Event()

    async def request():
        async with slots:
            if slots.active == min(capacity, requests):
                full.set()
            await release.wait()

    tasks = [asyncio.create_task(request()) for _ in range(requests)]
    await asyncio.wait_for(full.wait(), 10)
    peak = slots.snapshot()
    release.set()
    await asyncio.gather(*tasks)
    assert slots.active == slots.waiting == 0
    return {'requests': requests, 'completed': len(tasks), 'peak': peak, 'final': slots.snapshot()}


def measure(sessions=3000, workers=4, overflow=20):
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        raise SystemExit('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_capacity_' + uuid4().hex
    store = None
    started = time.monotonic()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-capacity-') as directory:
            settings = Settings(_env_file=None, data_dir=Path(directory) / 'coordinator',
                moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
                moyai_runtime_role='coordinator', temporal_enabled=True, object_storage_bucket='synthetic-only',
                session_secret=uuid4().hex, encryption_key=Fernet.generate_key().decode(),
                max_concurrent_runs=sessions, max_pending_runs=max(100, sessions + overflow),
                max_concurrent_model_requests=256, moyai_database_pool_size=16)
            store = open_store(settings)
            manager = DurableRunner(store, settings)
            ids = []
            for number in range(sessions + overflow):
                run = store.create_run('Synthetic capacity probe', '', 'demo', [], chat_enabled=True)
                manager.submit(run)
                ids.append(run['id'])
            # The real submission gate rejects the first item above its budget.
            try:
                store.create_run('Overflow', '', 'demo', [], chat_enabled=True)
            except ValueError as exc:
                assert 'queue is full' in str(exc)
            else:
                raise AssertionError('Pending-run budget was exceeded')
            seeded_ms = round((time.monotonic() - started) * 1000, 2)
            arguments = []
            for number in range(workers):
                options = settings.model_dump()
                options.update(moyai_runtime_role='worker', data_dir=Path(directory) / f'worker-{number}')
                arguments.append((options, ids[number::workers]))
            admission_started = time.monotonic()
            with multiprocessing.get_context('spawn').Pool(workers) as processes:
                results = processes.map(worker, arguments)
            admission_ms = round((time.monotonic() - admission_started) * 1000, 2)
            occupied = store.rows(f'SELECT COUNT(*) AS count FROM durable_sessions WHERE {manager.occupied_session}')[0]['count']
            admitted, queued = sum(r['admitted'] for r in results), sum(r['queued'] for r in results)
            assert admitted == occupied == sessions and queued == overflow
            assert len({r['pid'] for r in results}) == workers
            assert not store.rows('SELECT * FROM runtime_leases')
            return {'backend': 'postgresql', 'synthetic_provider_work': True,
                'sandbox_limit': sessions, 'submitted': len(ids), 'occupied': occupied,
                'admitted': admitted, 'queued': queued, 'pending_overflow_rejected': True,
                'worker_processes': workers, 'workers': results, 'pool_per_process': 16,
                'seed_ms': seeded_ms, 'admission_ms': admission_ms,
                'model': asyncio.run(model_probe(sessions, 256)), 'no_provider_calls': True}
    finally:
        if store:
            store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(schema)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=int, default=3000)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.sessions <= 10000 or not 1 <= args.workers <= 16:
        parser.error('Use 1–10000 sessions and 1–16 workers.')
    print(json.dumps(measure(args.sessions, args.workers), indent=2))
