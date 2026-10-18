"""Rotating credentials survive admission locks, competing workers and cancellation."""

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from app.connection_authority import authority
from app.connector_errors import ConnectorError
from app.connectors import Connectors
from app.db import Store, database
from app.security import Security
from app.slack_token_refresh import refresh_slack_token
from test_product_identity import settings_for


@pytest_asyncio.fixture(params=['sqlite', 'postgres'])
async def rotating(tmp_path, request):
    url = os.environ.get('MOYAI_TEST_POSTGRES') if request.param == 'postgres' else None
    if request.param == 'postgres' and not url:
        pytest.skip('Set a disposable local PostgreSQL URL.')
    schema = 'moyai_rotation_' + uuid4().hex
    if url:
        assert urlsplit(url).hostname in {'127.0.0.1', 'localhost', '::1'}
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(f'CREATE SCHEMA "{schema}"')
    settings = settings_for(tmp_path)
    store = await database(Store, tmp_path / 'source', database_url=url or '', database_schema=schema,
                           database_initialize=bool(url), application_instance=False, database_pool_size=3)
    security = Security(settings)
    first, second = [Connectors(store, security, settings) for _ in range(2)]
    credentials = {'access_token': 'old-user', 'refresh_token': 'rotate-user', 'expires_at': 1,
                   'bot': {'access_token': 'old-bot', 'refresh_token': 'rotate-bot', 'expires_at': 1,
                           'team': {'id': 'TLOCAL001'}, 'bot_user_id': 'UBOT00001'}}
    await database(first.save, 'slack', credentials, 'Synthetic installation')
    original = (await database(store.rows, "SELECT * FROM connections WHERE provider='slack'"))[0]
    calls = []

    async def exchange(provider, *, refresh_token):
        assert provider == 'slack'
        calls.append(refresh_token)
        # A rotating token can be consumed only once.
        assert calls.count(refresh_token) == 1
        await asyncio.sleep(0.03)
        kind = refresh_token.removeprefix('rotate-')
        return {'access_token': 'new-' + kind, 'refresh_token': 'next-' + kind,
                'expires_at': time.time() + 3600}

    first.exchange = second.exchange = exchange
    try:
        yield SimpleNamespace(first=first, second=second, store=store, security=security,
                              original=original, calls=calls, url=url, schema=schema, exchange=exchange)
    finally:
        await database(store.close)
        if url:
            with psycopg.connect(url, autocommit=True) as db:
                db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def saved(p):
    row = (await database(p.store.rows, "SELECT * FROM connections WHERE provider='slack'"))[0]
    value = json.loads(p.security.decrypt(row['encrypted']))
    previous = json.loads(p.security.decrypt(p.original['encrypted']))
    assert authority(value, row['encrypted']) == authority(previous, p.original['encrypted'])
    return value


async def test_independent_callers_rotate_each_token_once_without_losing_the_other_grant(rotating):
    p = rotating
    results = await asyncio.gather(p.first.slack_bot_token(), p.second.slack_bot_token(),
                                   p.first.credentials('slack'), p.second.credentials('slack'))
    assert results[:2] == ['new-bot', 'new-bot']
    assert [value['access_token'] for value in results[2:]] == ['new-user', 'new-user']
    value = await saved(p)
    assert value['access_token'] == 'new-user' and value['bot']['access_token'] == 'new-bot'
    assert value['bot']['team'] == {'id': 'TLOCAL001'}
    assert sorted(p.calls) == ['rotate-bot', 'rotate-user']


async def test_bot_refresh_completes_while_its_browser_holds_canonical_admission(rotating):
    p = rotating
    if not p.url:
        pytest.skip('Canonical admission uses PostgreSQL.')
    async with await psycopg.AsyncConnection.connect(p.url) as reader:
        await reader.execute('SELECT pg_advisory_xact_lock_shared(726941,%s::regnamespace::oid::int)',
                             (p.schema,))
        assert await asyncio.wait_for(p.first.slack_bot_token(), 2) == 'new-bot'
        assert (await saved(p))['bot']['refresh_token'] == 'next-bot'
        assert reader.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS


async def test_cancelled_viewer_still_persists_rotated_token_before_releasing_refresh_lock(rotating):
    p = rotating
    entered, release = threading.Event(), threading.Event()

    async def exchange(*args, **kwargs):
        result = await p.exchange(*args, **kwargs)
        entered.set()
        assert await asyncio.to_thread(release.wait, 3)
        return result

    p.first.exchange = exchange
    task = asyncio.create_task(p.first.slack_bot_token())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert (await saved(p))['bot']['refresh_token'] == 'next-bot'
    assert await p.second.slack_bot_token() == 'new-bot'
    assert p.calls == ['rotate-bot']


async def test_waiting_for_writer_does_not_consume_refresh_token(rotating):
    p = rotating
    if not p.url:
        pytest.skip('Canonical writer contention uses PostgreSQL.')
    task = None
    try:
        async with await psycopg.AsyncConnection.connect(p.url) as writer:
            await writer.execute('SELECT pg_advisory_xact_lock(726941,%s::regnamespace::oid::int)',
                                 (p.schema,))
            task = asyncio.create_task(p.first.slack_bot_token())
            await asyncio.sleep(0.1)
            assert not task.done() and p.calls == []
        assert await asyncio.wait_for(task, 2) == 'new-bot'
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('change', ['reconnect', 'delete'])
async def test_changed_installation_is_not_refreshed_or_resurrected(rotating, change):
    p = rotating
    if change == 'reconnect':
        await database(p.first.save, 'slack', {'access_token': 'replacement'}, 'Replacement')
    else:
        await database(p.store.execute, "DELETE FROM connections WHERE provider='slack'")
    with pytest.raises(ConnectorError) as error:
        await database(refresh_slack_token, p.first, p.original, bot=True)
    assert error.value.code == 'access_changed'
    assert p.calls == []
