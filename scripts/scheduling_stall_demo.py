"""Real broker/SQLite contention probe; no sandbox or model calls.

Run: uv run python scripts/scheduling_stall_demo.py
An independent writer holds SQLite for one second while the real control
endpoint is polled. A 10 ms heartbeat measures responsiveness of its event loop.
"""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import Settings
from app.main import create_app
from app.security import digest


async def probe(directory: Path, *, steering: bool, hold_seconds: float = 1) -> dict:
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=directory, public_url='http://127.0.0.1:8787', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store
    run = store.create_run('Local scheduling probe', '', 'modal', [], chat_enabled=True)
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('local-probe'))
    if steering:
        store.enqueue_message(run['id'], 'Local correction', 'correction', send_now=True)
    locked = threading.Event()

    def hold_writer():
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            locked.set()
            time.sleep(hold_seconds)

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
    await asyncio.to_thread(locked.wait)
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
        store.objects.close()
    return {'scenario': 'steering' if steering else 'empty_poll', 'writer_hold_ms': round(hold_seconds * 1000),
            'http_status': response.status_code, 'control_ms': round(duration * 1000, 2),
            'max_event_loop_lag_ms': round(max(ticks) * 1000, 2),
            'correction_delivered': bool(response.json().get('input'))}


async def main():
    with TemporaryDirectory(prefix='moyai-scheduling-') as directory:
        for steering in (False, True):
            print(json.dumps(await probe(Path(directory) / str(steering), steering=steering)), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
