"""Measure the real Slack watcher under a disposable database writer/pool wait.

Run: uv run python scripts/slack_polling_probe.py
Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database. Slack's HTTP
transport is local-only; this starts no real sandbox, model or Slack request.
"""
import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import Settings
from app.db import database
from app.main import create_app
from scripts.scheduling_stall_demo import postgres_target


async def probe(directory: Path, *, hold_seconds: float = 1, contention: str = 'writer',
                lane: str = 'both', database_url: str = '', database_schema: str = 'moyai') -> dict:
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=directory, public_url='http://127.0.0.1:8787', auto_prepare_repositories=False,
                  moyai_database_url=database_url, moyai_database_schema=database_schema,
                  moyai_database_initialize=True, slack_bot_enabled=True,
                  slack_signing_secret='local-probe-signing', slack_session_users='*')
    app = create_app(Settings(_env_file=None, **values))
    store, owner = app.state.store, app.state.slack
    chat = owner.chat
    if contention not in {'writer', 'pool'} or (contention == 'pool' and not store.database):
        store.close()
        raise ValueError('Pool contention requires PostgreSQL.')
    app.state.connectors.save('slack', {'bot': {'access_token': 'local-only-probe-token',
        'team': {'id': 'T12345678'}, 'bot_user_id': 'U99999999'}}, 'Local test')
    run = store.create_run('Local Slack polling probe', '', 'demo', [], chat_enabled=True)
    store.execute('''INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts)
        VALUES('T12345678','C12345678','1790719000.123456',?,'1790719000.123456')''', (run['id'],))
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'Local probe answer saved once.')
    store.update_run(run['id'], status='idle')
    if lane == 'media':
        with store.connect() as conn:
            chat.queue(conn, run['id'], 'answer:probe:media', 'answer', 'Local media-lane reply.')
    if lane == 'activity':
        store.update_run(run['id'], status='running')
    sent = []
    delivered = asyncio.Event()

    async def local_slack(method, url, **kwargs):
        assert method == 'POST' and url.startswith('https://slack.com/api/')
        sent.append(url.rsplit('/', 1)[-1])
        delivered.set()
        return {'ok': True, 'ts': '1790719999.123456'}

    app.state.connectors.request = local_slack
    locked, released = threading.Event(), threading.Event()
    errors = []

    def hold_database():
        try:
            with ExitStack() as held:
                if contention == 'pool':
                    for _ in range(store.database.pool.max_size):
                        held.enter_context(store.database.pool.connection())
                else:
                    held.enter_context(store.connect()).begin_write()
                locked.set()
                time.sleep(hold_seconds)
        except Exception as exc:
            errors.append(exc)
            locked.set()
        finally:
            released.set()

    ticks, health = [], []

    async def heartbeat():
        before = time.monotonic()
        while True:
            await asyncio.sleep(.01)
            current = time.monotonic()
            ticks.append(max(0, current - before - .01))
            before = current

    async def health_checks():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url']) as client:
            while True:
                started = time.monotonic()
                response = await client.get('/health')
                health.append({'ms': (time.monotonic() - started) * 1000,
                               'during_wait': not released.is_set(), 'status': response.status_code})
                await asyncio.sleep(.02)

    holder = threading.Thread(target=hold_database)
    holder.start()
    pulse = sampler = activity = None
    started = time.monotonic()
    try:
        if not await asyncio.to_thread(locked.wait, 10):
            raise TimeoutError('The local test database lock was not acquired.')
        if errors:
            raise errors[0]
        pulse = asyncio.create_task(heartbeat())
        await asyncio.sleep(.02)
        if lane in {'both', 'text'}:
            chat.watcher = asyncio.create_task(chat.watch())
        if lane in {'both', 'media'}:
            chat.media_watcher = asyncio.create_task(chat.watch(media=True))
        if lane == 'activity':
            activity = asyncio.create_task(chat.activity.sync())
        sampler = asyncio.create_task(health_checks())
        await asyncio.wait_for(delivered.wait(), hold_seconds + 10)
        await asyncio.sleep(.1)
        if activity:
            await activity
        await chat.shutdown()
        rows = await database(store.rows, "SELECT status FROM slack_outbox WHERE kind='answer'")
    finally:
        await chat.shutdown()
        tasks = [task for task in (pulse, sampler, activity) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(holder.join)
        await asyncio.to_thread(store.close)
    if errors:
        raise errors[0]
    return {'backend': 'postgresql' if store.database else 'sqlite', 'lane': lane,
            'contention': contention, 'hold_ms': round(hold_seconds * 1000),
            'elapsed_ms': round((time.monotonic() - started) * 1000, 2),
            'max_event_loop_lag_ms': round(max(ticks, default=0) * 1000, 2),
            'health_requests_during_wait': sum(row['during_wait'] for row in health),
            'health_requests': len(health), 'health_ok': all(row['status'] == 200 for row in health),
            'max_health_ms': round(max((row['ms'] for row in health), default=0), 2),
            'sent_answers': sum(row['status'] == 'sent' for row in rows),
            'uncertain_answers': sum(row['status'] == 'uncertain' for row in rows),
            'local_slack_calls': sent, 'external_provider_calls': 0}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hold-seconds', type=float, default=1)
    parser.add_argument('--contention', choices=('writer', 'pool'), default='writer')
    parser.add_argument('--lane', choices=('both', 'text', 'media', 'activity'), default='both')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-slack-poll-') as directory:
        with postgres_target(os.environ.get('MOYAI_TEST_POSTGRES_URL', '')) as target:
            result = await probe(Path(directory), hold_seconds=args.hold_seconds,
                                 contention=args.contention, lane=args.lane, **target)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
