"""Admission and fallback cleanup remain owned until database work settles."""
import asyncio
import json
import threading
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import JSONResponse
import pytest

from app.context_maintenance import ContextMaintenance
from app.db import Store
from app.model_slots import ModelSlots


@pytest.fixture
async def maintenance(tmp_path):
    store = Store(tmp_path, database_pool_size=1)
    run = store.create_run('Summarize public receipts.', '', 'demo', [])
    snapshot = {'epoch': 'a' * 32, 'cursor': 0, 'summary': '',
                'entries': [{'seq': 1, 'excerpt': 'A completed receipt'}]}
    calls = []

    async def read_body(*args):
        return {'snapshot': snapshot}

    async def flush():
        pass

    async def summarize(*args, **kwargs):
        calls.append(True)
        return JSONResponse({'summary': 'A completed receipt', 'through_seq': 1})

    store.execute('CREATE TABLE test_context_owners (id TEXT PRIMARY KEY)')

    class Owner:
        def claim_in(self, conn, kind, identity):
            conn.execute('INSERT INTO test_context_owners VALUES(?)', (identity,))

        def release_in(self, conn, kind, identity):
            conn.execute('DELETE FROM test_context_owners WHERE id=?', (identity,))

    store.broker_work = Owner()
    owner = ContextMaintenance(SimpleNamespace(store=store, checkpoints=SimpleNamespace(flush=flush),
        model_slots=ModelSlots(1), require_run=lambda *args: store.run(run['id']),
        read_body=read_body, summarize=summarize))
    try:
        yield owner, store, run, snapshot, calls
    finally:
        await owner.close()
        store.close()


async def test_shutdown_drains_an_admission_that_has_committed(maintenance, monkeypatch):
    owner, store, run, _, calls = maintenance
    entered, release = threading.Event(), threading.Event()
    claim = owner.claim

    def held(*args):
        result = claim(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(owner, 'claim', held)
    exchange = asyncio.create_task(owner.exchange(run['id'], None))
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        close = asyncio.create_task(owner.close())
        await asyncio.sleep(.02)
        close.cancel()
        await asyncio.sleep(.02)
        close.cancel()
        await asyncio.sleep(.02)
        assert not close.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    with pytest.raises(asyncio.CancelledError):
        await exchange
    assert owner.read(run['id'])['status'] == 'interrupted'
    assert not calls and not store.rows('SELECT * FROM test_context_owners')
    assert not owner.tasks and not owner.admissions and not owner.cleanups
    with pytest.raises(HTTPException) as rejected:
        await owner.exchange(run['id'], None)
    assert rejected.value.status_code == 503


async def test_shutdown_waits_for_cleanup_of_an_unstarted_generator(maintenance, monkeypatch):
    owner, store, run, _, calls = maintenance
    run_maintenance = owner.gateway.model_slots.run_maintenance
    entered, release = threading.Event(), threading.Event()
    settle = owner.settle

    def cancel_before_start(coroutine):
        task = run_maintenance(coroutine)
        task.cancel()
        return task

    def held(*args, **kwargs):
        if kwargs.get('status') is None:
            entered.set()
            assert release.wait(5)
        return settle(*args, **kwargs)

    monkeypatch.setattr(owner.gateway.model_slots, 'run_maintenance', cancel_before_start)
    monkeypatch.setattr(owner, 'settle', held)
    await owner.exchange(run['id'], None)
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        close = asyncio.create_task(owner.close())
        await asyncio.sleep(.03)
        assert not close.done()
    finally:
        release.set()
    await close
    assert owner.read(run['id'])['status'] == 'interrupted'
    assert not calls and not store.rows('SELECT * FROM test_context_owners')


async def test_late_cleanup_preserves_completed_and_replacement_jobs(maintenance):
    owner, store, run, snapshot, _ = maintenance
    serialized = json.dumps(snapshot)
    owner.claim(run['id'], 'old', serialized, '', run, None)
    owner.settle(run['id'], 'old', status='completed', result={'summary': 'retained'})
    await owner.cleanup(run['id'], 'old')
    assert owner.read(run['id'])['result'] == {'summary': 'retained'}
    owner.claim(run['id'], 'new', serialized, 'old', run, None)
    await owner.cleanup(run['id'], 'old')
    assert owner.read(run['id'])['id'] == 'new'
    assert owner.read(run['id'])['status'] == 'running'
    assert store.rows('SELECT * FROM test_context_owners') == [{'id': 'new'}]
    await owner.cleanup(run['id'], 'new')


async def test_competing_admission_reads_its_receipt_on_the_same_connection(maintenance):
    owner, _, run, snapshot, _ = maintenance
    serialized = json.dumps(snapshot)
    first, claimed = owner.claim(run['id'], 'first', serialized, '', run, None)
    assert claimed
    second, claimed = owner.claim(run['id'], 'second', serialized, '', run, None)
    assert not claimed and second == first
    await owner.cleanup(run['id'], 'first')


@pytest.mark.parametrize('failure', ['cancelled', 'failed', 'permanent'])
async def test_final_checkpoint_remains_owned_until_durable_or_reported(maintenance, monkeypatch, failure):
    owner, store, run, _, _ = maintenance
    flushing, fallback, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = 0

    async def flush():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return  # Admission checkpoint.
        if attempts == 2:
            flushing.set()
            if failure == 'cancelled':
                await asyncio.Future()
            raise OSError('final checkpoint unavailable')
        fallback.set()
        await release.wait()
        if failure == 'permanent':
            raise OSError('fallback checkpoint unavailable')

    monkeypatch.setattr(owner.gateway.checkpoints, 'flush', flush)
    await owner.exchange(run['id'], None)
    await asyncio.wait_for(flushing.wait(), 2)
    close = asyncio.create_task(owner.close())
    try:
        await asyncio.wait_for(fallback.wait(), 2)
        assert not close.done()
        assert owner.read(run['id'])['status'] == 'completed'
    finally:
        release.set()
    if failure == 'permanent':
        with pytest.raises(OSError, match='fallback checkpoint'):
            await close
        owner.cleanup_errors.clear()
        owner.close_task = None  # The fixture may now close an already drained owner.
    else:
        await close
    assert attempts == 3 and not owner.cleanups
    assert not store.rows('SELECT * FROM test_context_owners')
    assert owner.read(run['id'])['result']['summary'] == 'A completed receipt'
