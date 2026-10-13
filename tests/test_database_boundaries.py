"""Connection ownership is explicit across helpers, threads and callbacks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.database import DatabaseBusyError, NestedDatabaseConnectionError
from app.db import Store, database


@pytest.fixture
def store(tmp_path):
    current = Store(tmp_path)
    try:
        yield current
    finally:
        current.close()


def test_nested_checkout_is_a_programming_error_and_existing_connection_works(store):
    with store.connect() as conn:
        conn.begin_write()
        with pytest.raises(NestedDatabaseConnectionError, match='Pass the existing connection') as failure:
            store.rows('SELECT 1 AS value')
        assert not isinstance(failure.value, DatabaseBusyError)
        assert store.rows('SELECT 1 AS value', connection=conn) == [{'value': 1}]
    assert store.rows('SELECT 2 AS value') == [{'value': 2}]


def test_nested_failure_rolls_back_and_releases_ownership(store):
    run = store.create_run('A transaction rollback check', '', 'demo', [])
    callbacks = []
    with pytest.raises(NestedDatabaseConnectionError):
        with store.connect() as conn:
            conn.execute('UPDATE runs SET summary=? WHERE id=?', ('must roll back', run['id']))
            conn.commit_callbacks.append(lambda: callbacks.append('must not run'))
            store.run(run['id'])
    assert store.run(run['id'])['summary'] == ''
    assert callbacks == []
    store.update_run(run['id'], summary='new transaction')
    assert store.run(run['id'])['summary'] == 'new transaction'


def test_commit_callbacks_can_borrow_after_the_committed_connection_is_released(store):
    run = store.create_run('Post-commit callback check', '', 'demo', [])
    observed = []
    listener = lambda: observed.append(('listener', store.run(run['id'])['summary']))
    store.commit_listeners.add(listener)
    try:
        with store.connect() as conn:
            conn.execute('UPDATE runs SET summary=? WHERE id=?', ('committed', run['id']))
            conn.commit_callbacks.append(lambda: observed.append(
                ('callback', store.run(run['id'])['summary'])))
        assert observed == [('listener', 'committed'), ('callback', 'committed')]
    finally:
        store.commit_listeners.remove(listener)


def test_independent_thread_is_not_mistaken_for_a_nested_checkout(store):
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.connect() as conn:
            conn.execute('SELECT 1')
            assert executor.submit(store.rows, 'SELECT 2 AS value').result(timeout=2) == [{'value': 2}]


async def test_task_inheriting_a_finished_scope_can_start_a_new_transaction(store):
    release = asyncio.Event()

    async def later():
        await release.wait()
        return await asyncio.to_thread(store.rows, 'SELECT 3 AS value')

    with store.connect() as conn:
        conn.execute('SELECT 1')
        task = asyncio.create_task(later())
    release.set()
    assert await asyncio.wait_for(task, 2) == [{'value': 3}]


async def test_offloading_does_not_hide_a_nested_borrow_from_the_same_operation(store):
    with store.connect():
        with pytest.raises(NestedDatabaseConnectionError):
            await asyncio.to_thread(store.rows, 'SELECT 1')
    assert await asyncio.to_thread(store.rows, 'SELECT 4 AS value') == [{'value': 4}]


def test_a_different_store_does_not_share_the_checkout_guard(store, tmp_path):
    other = Store(tmp_path / 'independent')
    try:
        with store.connect() as conn:
            conn.execute('SELECT 1')
            assert other.rows('SELECT 5 AS value') == [{'value': 5}]
    finally:
        other.close()


@pytest.mark.parametrize('fail', [False, True])
async def test_repeated_cancellation_drains_the_database_operation(store, fail):
    entered, release = threading.Event(), threading.Event()
    run = store.create_run('Drain a committed operation', '', 'demo', [])

    def write():
        with store.connect() as conn:
            conn.execute('UPDATE runs SET summary=? WHERE id=?', ('saved', run['id']))
            entered.set()
            assert release.wait(5)
            if fail:
                raise RuntimeError('transaction failed')

    task = asyncio.create_task(database(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel('first cancellation')
        await asyncio.sleep(.02)
        task.cancel('second cancellation')
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.args == ('first cancellation',)
    if fail:
        assert isinstance(cancelled.value.__cause__, RuntimeError)
    assert store.run(run['id'])['summary'] == ('' if fail else 'saved')
