"""Exercise broker self-healing over real HTTP and disposable local PostgreSQL.

MOYAI_TEST_POSTGRES_URL=postgresql://... uv run python scripts/broker_recovery_probe.py
Only Temporal's remote connection is substituted; ownership, locks, pool waits,
HTTP, cancellation and server shutdown use the actual application code.
"""
import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager
import logging
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
import uvicorn
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.main import create_app
from app.schema_migrations import migrate
from app.temporal_runtime import TemporalRunManager
from render_start import RenderOverlapServer


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@asynccontextmanager
async def serving(app):
    server = RenderOverlapServer(uvicorn.Config(app, host='127.0.0.1', port=0,
        log_level='critical', timeout_graceful_shutdown=3, proxy_headers=False),
        failure_check=lambda: app.state.broker_requests.failed)
    task = asyncio.create_task(server.serve())
    try:
        await until(lambda: server.started or task.done())
        assert not task.done(), 'Broker startup failed'
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                                     headers={'Host': '127.0.0.1:8787'}) as client:
            yield server, client, task
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)


async def probe(url, pause):
    schema = 'moyai_recovery_' + uuid4().hex
    apps = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with TemporaryDirectory(prefix='moyai-broker-recovery-') as directory, patch.object(
                TemporalRunManager, 'connect_temporal', AsyncMock(return_value=SimpleNamespace(
                    start_workflow=AsyncMock()))):
            settings = Settings(_env_file=None, data_dir=Path(directory), public_url='http://127.0.0.1:8787',
                moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
                moyai_runtime_role='coordinator', moyai_separate_broker=True, moyai_build_sha='a' * 40,
                temporal_enabled=True, temporal_tls=False, object_storage_bucket='synthetic-recovery-probe',
                session_secret='synthetic-recovery-probe', encryption_key=Fernet.generate_key().decode(),
                session_titles_enabled=False, memory_review_enabled=False, litellm_spend_recovery_enabled=False)
            migrate(settings)
            settings = settings.model_copy(update={'moyai_schema_mode': 'verify'})
            apps.append(create_app(settings))

            def broker():
                app = create_app(settings.model_copy(update={'moyai_runtime_role': 'broker',
                    'data_dir': Path(directory) / f'broker-{len(apps)}'}))
                apps.append(app)
                return app

            app = broker()
            owner = app.state.broker_requests
            pool = app.state.store.database.pool
            pool.timeout = .15
            entered, finish = asyncio.Event(), asyncio.Event()
            calls = 0

            @app.get('/broker/recovery-probe')
            async def request():
                nonlocal calls
                calls += 1
                entered.set()
                await finish.wait()
                return {'status': 'completed', 'calls': calls}

            print('BROKER SELF-HEALING | local PostgreSQL + real HTTP', flush=True)
            print('No production data, providers or connected apps are used.', flush=True)
            async with serving(app) as (server, client, stopped):
                assert (await client.get('/health')).status_code == 200
                print('1. Healthy broker: GET /health -> 200', flush=True)
                await asyncio.sleep(pause)
                active = asyncio.create_task(client.get('/broker/recovery-probe', timeout=10))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    with ExitStack() as held:
                        for _ in range(pool.max_size):
                            held.enter_context(pool.connection())
                        print(f'2. Hold all {pool.max_size} database pool connections.', flush=True)
                        await until(lambda: owner.recovery_busy)
                        await asyncio.sleep(max(pause, 1))
                        assert not owner.failed and not active.done() and not server.retired
                        print('   Pool timeout observed; active request survives; ownership intact.', flush=True)
                    finish.set()
                    response = await active
                    assert response.status_code == 200 and response.json()['calls'] == 1
                    pool.timeout = 10
                    await until(lambda: not owner.recovery_busy)
                    # Dispatch readiness has its own bounded reconnect loop.
                    # A pooled health request may remain unavailable until it
                    # observes the recovered database on its next attempt.
                    await until(app.state.manager.ready.is_set)
                    health = await client.get('/health')
                    assert health.status_code == 200, health.text
                    print('3. Release pool: request completes ONCE; health -> 200, no restart.', flush=True)
                    await asyncio.sleep(pause)
                    app.state.store.database.owner.close()
                    print('4. Lose the real PostgreSQL ownership connection.', flush=True)
                    await asyncio.wait_for(asyncio.shield(stopped), 5)
                    assert server.retired and app.state.model_slots._closed
                    print('   Failed broker drained and exited; ready for platform replacement.', flush=True)
                finally:
                    finish.set()
                    await asyncio.gather(active, return_exceptions=True)
            await asyncio.sleep(pause)
            async with serving(broker()) as (_, client, _):
                assert (await client.get('/health')).status_code == 200
                print('5. Fresh broker on the same schema: GET /health -> 200', flush=True)
                print('PASS: transient recovery + safe retirement + healthy replacement.', flush=True)
                await asyncio.sleep(pause)
    finally:
        for app in apps:
            if app.state.broker_requests:
                app.state.model_slots.close()
            app.state.store.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause', type=float, default=0, help='Seconds to hold each demonstration stage (0–4).')
    args = parser.parse_args()
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL', '')
    if urlsplit(url).hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable local PostgreSQL database.')
    if not 0 <= args.pause <= 4:
        parser.error('--pause must be between 0 and 4 seconds.')
    logging.disable(logging.CRITICAL)
    asyncio.run(probe(url, args.pause))
