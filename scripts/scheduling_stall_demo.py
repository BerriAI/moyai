"""Real broker/database contention probe; no sandbox or model calls.

Run: uv run python scripts/scheduling_stall_demo.py
Set MOYAI_TEST_POSTGRES_URL to a disposable database to probe PostgreSQL instead.
An independent writer holds the backend's write lock for one second. Postgres
also tests a full connection pool. A 10 ms heartbeat measures event-loop delay.
"""
import asyncio
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import Settings
from app.main import create_app
from app.security import digest


@contextmanager
def postgres_target(url):
    """Only create and drop our own random schema in the supplied test database."""
    if not url:
        yield {}
        return
    import psycopg
    schema = 'moyai_scheduling_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield {'database_url': url, 'database_schema': schema}
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def probe(directory: Path, *, steering: bool, hold_seconds: float = 1,
                contention: str = 'writer', database_url: str = '', database_schema: str = 'moyai') -> dict:
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=directory, public_url='http://127.0.0.1:8787', auto_prepare_repositories=False,
                  moyai_database_url=database_url, moyai_database_schema=database_schema,
                  moyai_database_initialize=True)
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    backend = 'postgresql' if store.database else 'sqlite'
    if contention not in {'writer', 'pool'} or (contention == 'pool' and not store.database):
        store.close()
        raise ValueError('Pool contention requires PostgreSQL; otherwise use writer contention.')
    run = store.create_run('Local scheduling probe', '', 'modal', [], chat_enabled=True)
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('local-probe'))
    if steering:
        store.enqueue_message(run['id'], 'Local correction', 'correction', send_now=True)
    locked = threading.Event()
    errors = []

    def hold_writer():
        try:
            with ExitStack() as held:
                if contention == 'pool':
                    for _ in range(store.database.pool.max_size):
                        held.enter_context(store.database.pool.connection())
                else:
                    conn = held.enter_context(store.connect())
                    conn.begin_write()
                locked.set()
                time.sleep(hold_seconds)
        except Exception as exc:
            errors.append(exc)
            locked.set()

    ticks = []
    async def heartbeat():
        before = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            current = time.monotonic()
            ticks.append(max(0, current - before - 0.01))
            before = current

    writer = threading.Thread(target=hold_writer)
    writer.start()
    if not await asyncio.to_thread(locked.wait, 15):
        raise TimeoutError('The contention probe could not acquire its test lock.')
    if errors:
        store.close()
        raise errors[0]
    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 50000)),
                                    base_url=values['public_url']) as client:
            response = await client.post(f"/broker/{run['id']}/control",
                headers={'Authorization': 'Bearer local-probe'}, json={'version': 2, 'applied': []})
            duration = time.monotonic() - started
            response.raise_for_status()
        await asyncio.sleep(0.03)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        await asyncio.to_thread(writer.join)
        store.close()
    if errors:
        raise errors[0]
    return {'backend': backend, 'contention': contention, 'scenario': 'steering' if steering else 'empty_poll',
            'hold_ms': round(hold_seconds * 1000), 'sqlite_file_created': store.path.exists(),
            'http_status': response.status_code, 'control_ms': round(duration * 1000, 2),
            'max_event_loop_lag_ms': round(max(ticks) * 1000, 2),
            'correction_delivered': bool(response.json().get('input'))}


async def main():
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL', '')
    with TemporaryDirectory(prefix='moyai-scheduling-') as directory:
        for contention in (('writer', 'pool') if url else ('writer',)):
            for steering in (False, True):
                with postgres_target(url) as target:
                    result = await probe(Path(directory) / contention / str(steering), steering=steering,
                                         contention=contention, **target)
                    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
