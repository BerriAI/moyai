"""Real PostgreSQL admission while one synthetic sandbox termination is slow.

Two worker stores share real database leases. Provider cleanup and execution
are simulated; no sandboxes or models are called. Only a random schema is used.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.db import database
from app.durable_runner import DurableRunner
from scripts.runtime_capacity_probe import open_store


async def measure(hold_seconds=5):
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        raise SystemExit('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_cleanup_' + uuid4().hex
    stores, tasks = [], []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-cleanup-') as directory:
            settings = Settings(_env_file=None, data_dir=Path(directory), moyai_database_url=url,
                moyai_database_schema=schema, moyai_database_initialize=True, temporal_enabled=True,
                moyai_runtime_role='coordinator', object_storage_bucket='synthetic-only',
                session_secret=uuid4().hex, encryption_key=Fernet.generate_key().decode(),
                max_concurrent_runs=2)
            store = open_store(settings)
            stores.append(store)
            workers = []
            for index in range(2):
                options = settings.model_copy(update={'moyai_runtime_role': 'worker',
                                                      'data_dir': Path(directory) / str(index)})
                worker_store = open_store(options)
                stores.append(worker_store)
                workers.append(DurableRunner(worker_store, options))
            first, second = workers

            def session(warm=False, order=0):
                run = store.create_run('Synthetic cleanup capacity', '', 'demo', [], chat_enabled=not warm)
                first.submit(run)
                if warm:
                    first.save(run['id'], {'phase': 'warm', 'sandbox_id': 'synthetic-' + run['id'],
                        'machine_started': time.time(), 'idle_until': time.time() + 600 + order})
                return run['id']

            slow, fast = session(True), session(True, 1)
            waiting, independent, overflow = [session() for _ in range(3)]
            entered = asyncio.Event()
            cleaned = []

            async def cleanup(state, run_id):
                if run_id == slow:
                    entered.set()
                    await asyncio.sleep(hold_seconds)
                cleaned.append(run_id)

            async def step(run_id, state):
                return True  # Keep the real reservation; no execution provider.

            for worker in workers:
                worker.cleanup, worker.step = cleanup, step

            def occupied():
                return len(store.rows(f'SELECT run_id FROM durable_sessions WHERE {first.occupied_session}'))

            task = asyncio.create_task(first.advance(waiting))
            tasks.append(task)
            await asyncio.wait_for(entered.wait(), 10)
            initial_occupied = await database(occupied)
            assert initial_occupied == 2 and first.state(slow)['phase'] == 'warm_cleanup'
            started = time.monotonic()
            assert await asyncio.wait_for(second.advance(independent), hold_seconds + 10) is True
            elapsed_ms = (time.monotonic() - started) * 1000
            independent_before_cleanup = slow not in cleaned
            during_occupied = await database(occupied)
            assert await asyncio.wait_for(second.advance(overflow), 10) == 'capacity'
            assert store.has_queued_messages(overflow)
            assert await asyncio.wait_for(task, hold_seconds + 10) is True
            final_occupied = await database(occupied)
            assert initial_occupied == during_occupied == final_occupied == 2
            assert len(cleaned) == len(set(cleaned)) == 2
            assert not store.rows('SELECT 1 FROM runtime_leases')
            return {'backend': 'postgresql', 'worker_stores': 2, 'sandbox_limit': 2,
                'synthetic_cleanup_seconds': hold_seconds,
                'independent_admission_ms': round(elapsed_ms, 2),
                'admitted_before_slow_cleanup_finished': independent_before_cleanup,
                'occupied_before_during_after': [initial_occupied, during_occupied, final_occupied],
                'overflow_queued': True, 'each_sandbox_cleaned_once': True,
                'synthetic_provider_work': True, 'no_provider_calls': True}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for store in reversed(stores):
            store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(schema)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hold-seconds', type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.hold_seconds <= 15:
        parser.error('Use a simulated cleanup delay between 1 and 15 seconds.')
    print(json.dumps(asyncio.run(measure(args.hold_seconds)), indent=2))
