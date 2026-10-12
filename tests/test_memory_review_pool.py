import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import threading

import httpx
import pytest
from fastapi import HTTPException

from app.db import now
from app.security import digest
from test_memory_review import completion, jobs, process, proposal, review_app, turn
from test_skill_learning import enable, process as process_skills, turn as skill_turn


@contextmanager
def remaining_pool_slot(store):
    if not store.database:
        pytest.skip('Pool contention requires the real PostgreSQL backend.')
    pool = store.database.pool
    pool.timeout = .3
    with ExitStack() as occupied:
        for _index in range(pool.max_size - 1):
            occupied.enter_context(pool.connection())
        yield


def test_turn_completion_uses_one_available_pool_connection(review_app):
    store = review_app.state.store
    with remaining_pool_slot(store):
        _, message_id = turn(review_app)
    assert jobs(review_app)[0]['message_id'] == message_id
    assert store.database.pool.get_stats().get('requests_errors', 0) == 0


def test_review_backfill_uses_one_available_pool_connection(review_app):
    store = review_app.state.store
    reviewer = store.memory_review
    store.memory_review = None
    _, message_id = turn(review_app)
    store.memory_review = reviewer
    with remaining_pool_slot(store):
        reviewer.backfill()
    assert jobs(review_app)[0]['message_id'] == message_id
    assert store.database.pool.get_stats().get('requests_errors', 0) == 0


async def test_background_review_uses_one_available_pool_connection(review_app):
    store = review_app.state.store
    turn(review_app)
    with remaining_pool_slot(store):
        assert await process(review_app, lambda request: completion([proposal(request)]))
    assert jobs(review_app)[0]['status'] == 'completed'
    assert jobs(review_app)[0]['saved_count'] == 1
    assert len(review_app.state.memory.listing('google:alice')) == 1
    assert store.database.pool.get_stats().get('requests_errors', 0) == 0


async def test_skill_review_uses_one_available_pool_connection(review_app):
    store = review_app.state.store
    enable(review_app)
    with remaining_pool_slot(store):
        _, message_id = skill_turn(review_app)
        assert await process_skills(review_app)
    rows = store.rows('SELECT status FROM skill_learning_jobs WHERE message_id=?', (message_id,))
    assert rows == [{'status': 'completed'}]
    assert len(review_app.state.skill_learning.listing('google:alice')['suggestions']) == 1
    assert store.database.pool.get_stats().get('requests_errors', 0) == 0


@pytest.mark.parametrize('writer_kind', ['backfill', 'completion'])
async def test_review_writer_does_not_starve_competing_writers_and_catalog(review_app, monkeypatch, writer_kind):
    store = review_app.state.store
    if not store.database:
        pytest.skip('Pool contention requires the real PostgreSQL backend.')
    if writer_kind == 'completion':
        runs = [store.create_run('Remember my benchmark preferences.', '', 'modal', [],
            chat_enabled=True, user_id='google:alice', model=review_app.state.settings.agent_model)
            for _index in range(5)]
        messages = [store.claim_message(run['id']) for run in runs]
        run = runs[0]
    else:
        run, _message_id = turn(review_app)
    store.update_run(run['id'], status='running', token_hash=digest('pool-regression'))
    reviewer = review_app.state.memory_review
    pool = store.database.pool
    pool.timeout = 1
    holder_count = len(runs) if writer_kind == 'completion' else 1
    assert pool.max_size > holder_count
    holders_ready = threading.Barrier(holder_count + 1)
    proceed = threading.Event()
    admitted = [threading.Event() for _index in range(pool.max_size - holder_count)]
    original = reviewer.allowed_in

    def pause_with_writer_lock(*args, **kwargs):
        holders_ready.wait(5)
        assert proceed.wait(5)
        return original(*args, **kwargs)

    def competing_writer(entered):
        with store.connect() as connection:
            entered.set()
            connection.begin_write()
            return connection.execute('SELECT 1').fetchone()[0]

    monkeypatch.setattr(reviewer, 'allowed_in', pause_with_writer_lock)
    with ThreadPoolExecutor(max_workers=pool.max_size) as executor:
        if writer_kind == 'completion':
            holders = [executor.submit(store.finish_message, run['id'], message['id'], 'Task answer.')
                       for run, message in zip(runs, messages)]
        else:
            holders = [executor.submit(reviewer.backfill)]
        requests = []
        catalog = None
        try:
            await asyncio.to_thread(holders_ready.wait, 5)
            requests = [executor.submit(competing_writer, entered) for entered in admitted]
            for entered in admitted:
                assert await asyncio.to_thread(entered.wait, 5)
            async with asyncio.timeout(5):
                while True:
                    locks = await asyncio.to_thread(lambda: store.database.owner.execute('''SELECT mode,granted,count(*)
                        FROM pg_locks WHERE locktype='advisory' AND classid=726941
                        AND objid=%s::regnamespace::oid AND objsubid=2 GROUP BY mode,granted''',
                        (store.database.schema,)).fetchall())
                    counts = {(mode, granted): count for mode, granted, count in locks}
                    if counts.get(('ExclusiveLock', False)) == len(admitted):
                        break
                    await asyncio.sleep(.01)
            holder_mode = 'ShareLock' if writer_kind == 'completion' else 'ExclusiveLock'
            assert counts[(holder_mode, True)] == holder_count
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=review_app),
                                         base_url=review_app.state.settings.public_url) as client:
                catalog = asyncio.create_task(client.get(f"/broker/{run['id']}/tools",
                    headers={'Authorization': 'Bearer pool-regression'}))
                async with asyncio.timeout(5):
                    while not pool.get_stats().get('requests_waiting'):
                        await asyncio.sleep(.01)
                assert not catalog.done()
                proceed.set()
                response = await asyncio.wait_for(catalog, 5)
                assert response.status_code == 200, response.text
                assert any(tool['name'] == 'history_read' for tool in response.json())
        finally:
            proceed.set()
            if catalog is not None and not catalog.done():
                catalog.cancel()
                await asyncio.gather(catalog, return_exceptions=True)
            for holder in holders:
                holder.result(timeout=5)
            assert [request.result(timeout=5) for request in requests] == [1] * len(requests)
    assert pool.get_stats().get('requests_errors', 0) == 0


@pytest.mark.parametrize('profile', ['fresh', 'expired', 'conflicted', 'different_email'])
def test_requester_verification_reuses_its_transaction(review_app, monkeypatch, profile):
    store = review_app.state.store

    def no_nested_connection(*_args, **_kwargs):
        raise AssertionError('Requester verification borrowed another connection')

    with store.connect() as connection:
        actor = store.slack_identity_in(connection, 'T123', 'U123')
        connection.execute('''UPDATE users SET email=?,profile_eligible=1,profile_checked_at=?,
            profile_conflict=?,linked_user_id='google:alice' WHERE id=?''',
            ('different@berri.ai' if profile == 'different_email' else 'alice@berri.ai',
             '2000-01-01T00:00:00+00:00' if profile == 'expired' else now(),
             int(profile == 'conflicted'), actor))
        with monkeypatch.context() as guard:
            guard.setattr(store, 'connect', no_nested_connection)
            if profile == 'fresh':
                assert review_app.state.memory.owner(actor, connection) == 'google:alice'
            else:
                with pytest.raises(HTTPException) as rejected:
                    review_app.state.memory.owner(actor, connection)
                assert rejected.value.status_code == 403
