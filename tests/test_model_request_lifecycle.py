"""Accounting stays owned when an async caller loses a committed return value."""
import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.model_request import ModelRequest
from app.spend import Spend


@pytest.fixture
def accounting(tmp_path):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, data_dir=tmp_path, litellm_api_key='test-key')
    run = store.create_run('An accounting test', '', 'modal', [], chat_enabled=True, user_id='owner')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='capability')
    run = store.run(run['id'])
    events = []

    async def flush():
        events.append('flush')

    checkpoints = SimpleNamespace(flush=flush)
    spend = Spend(store, settings, None, checkpoints)
    store.execute('CREATE TABLE test_model_owners (id TEXT PRIMARY KEY)')

    class Owner:
        def claim_in(self, conn, kind, identity):
            conn.execute('INSERT INTO test_model_owners VALUES(?)', (identity,))

        def release_in(self, conn, kind, identity):
            conn.execute('DELETE FROM test_model_owners WHERE id=?', (identity,))

    store.broker_work = Owner()
    traces = []
    request = ModelRequest(settings=settings, store=store, spend=spend,
        tracing=SimpleNamespace(model=lambda *args, **kwargs: traces.append((args, kwargs))),
        checkpoints=checkpoints, run=run, model='test-model', started=time.time_ns())
    try:
        yield request, store, events, traces
    finally:
        store.close()


async def test_failed_ownership_claim_rolls_back_counter_and_spend_together(accounting, monkeypatch):
    request, store, _, traces = accounting

    def fail(*args):
        raise RuntimeError('owner claim failed')

    monkeypatch.setattr(store.broker_work, 'claim_in', fail)
    with pytest.raises(RuntimeError, match='owner claim failed'):
        await request.admit(lambda: None, startup=True)
    await request.finish(None, 'failed', [], {}, error={})
    assert store.run(request.run['id'])['model_calls'] == 0
    assert not store.rows('SELECT * FROM model_requests')
    assert not traces


async def test_cancel_after_admission_commit_keeps_receipt_for_cleanup(accounting, monkeypatch):
    request, store, events, traces = accounting
    entered, release = threading.Event(), threading.Event()
    admit = request.admit_in

    def committed(validate, startup):
        admit(validate, startup)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(request, 'admit_in', committed)

    async def route():
        try:
            await request.admit(lambda: None, startup=True)
            pytest.fail('A cancelled admission must not dispatch inference')
        finally:
            await request.finish(None, 'interrupted', [], {}, error={})

    pending = asyncio.create_task(route())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(.02)
        pending.cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert store.rows('SELECT id,status FROM model_requests') == [
        {'id': request.id, 'status': 'interrupted'}]
    assert not store.rows('SELECT * FROM test_model_owners')
    assert len(traces) == 1 and events == ['flush']


async def test_repeated_cancellation_during_finish_preserves_confirmed_completion(accounting, monkeypatch):
    request, store, events, traces = accounting
    await request.admit(lambda: None, startup=True)
    entered, release = threading.Event(), threading.Event()
    finish = request.spend.finish

    def committed(*args):
        result = finish(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(request.spend, 'finish', committed)
    capture = SimpleNamespace(usage={'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}, cost='0.25')
    pending = asyncio.create_task(request.finish(capture, 'completed', [], {}, error={}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(.02)
        pending.cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await request.finish(None, 'interrupted', [], {}, error={'error_code': 'cancelled'})
    assert store.rows('SELECT status,cost,total_tokens FROM model_requests') == [
        {'status': 'completed', 'cost': '0.25', 'total_tokens': 14}]
    assert len(traces) == 1 and traces[0][0][5] == 'completed'
    assert events == ['flush'] and not store.rows('SELECT * FROM test_model_owners')


async def test_transport_close_failure_does_not_skip_other_cleanup(accounting):
    request, store, events, traces = accounting
    await request.admit(lambda: None, startup=True)

    async def upstream():
        events.append('upstream')
        raise OSError('close failed')

    async def client():
        events.append('client')

    with pytest.raises(OSError, match='close failed'):
        await request.finish(None, 'completed', [], {}, error={}, close=[upstream, client])
    assert events == ['upstream', 'client', 'flush']
    assert store.rows('SELECT status FROM model_requests') == [{'status': 'completed'}]
    assert len(traces) == 1 and not store.rows('SELECT * FROM test_model_owners')


async def test_cancelled_header_persistence_keeps_gateway_correlation(accounting, monkeypatch):
    request, store, _, traces = accounting
    await request.admit(lambda: None, startup=True)
    entered, release = threading.Event(), threading.Event()
    headers = request.spend.headers

    def committed(*args):
        result = headers(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(request.spend, 'headers', committed)
    upstream = httpx.Response(200, headers={'x-litellm-call-id': 'gateway-receipt'})
    pending = asyncio.create_task(request.headers(upstream, False))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(.02)
        pending.cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await request.finish(None, 'interrupted', [], {}, error={})
    assert traces[0][1]['gateway_id'] == 'gateway-receipt'
    assert store.rows('SELECT gateway_id,status FROM model_requests') == [
        {'gateway_id': 'gateway-receipt', 'status': 'interrupted'}]
