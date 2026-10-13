"""Goal source reads must not borrow a second connection or fence other chats."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
from app.goals import Goals, GoalArgs
from app.session_journal import SessionJournal
from test_postgres_runtime import target  # noqa: F401


@pytest.fixture
def goal_store(tmp_path, target):
    url, schema = target
    store = Store(tmp_path, database_url=url, database_schema=schema,
                  database_initialize=True, database_pool_size=1)
    run = store.create_run('Work until the checks pass.', '', 'demo', [],
                           chat_enabled=True, user_id='owner')
    message = store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='test-capability')
    service = store.goals = Goals(store)
    SessionJournal(store)
    # A regression should fail promptly instead of waiting the production 10s.
    store.database.pool.timeout = .3
    try:
        yield store, service, store.run(run['id']), message
    finally:
        store.close()


@pytest.mark.parametrize('internal', [False, True])
def test_goal_get_uses_the_only_pool_connection(goal_store, internal):
    store, service, caller, message = goal_store
    result = service.exchange(caller, {'action': 'get'}, internal=internal)
    assert result['goal'] is None
    assert [(item['id'], item['content']) for item in result['inputs']] == [
        (message['id'], 'Work until the checks pass.')]
    assert store.database.pool.get_stats().get('requests_waiting', 0) == 0


def test_goal_writes_and_review_reuse_their_connection(goal_store):
    _, service, caller, message = goal_store
    created = service.exchange(caller, {'action': 'create', 'source_input_id': message['id'],
        'objective': 'Verify the checks.'})['goal']
    ticked = service.exchange(caller, {'action': 'tick', 'segment': 'first', 'elapsed': 2},
                             internal=True)['goal']
    assert ticked['elapsed_seconds'] == 2
    snapshot = service.review_snapshot(caller, GoalArgs(action='complete', note='Done'))
    assert snapshot['goal']['id'] == created['id']
    assert snapshot['authenticated_inputs'][0]['id'] == message['id']


@pytest.mark.parametrize('internal', [False, True])
def test_goal_get_reads_committed_state_while_another_writer_is_busy(goal_store, target, internal):
    store, service, caller, _ = goal_store
    url, schema = target
    other = Store(store.path.parent / 'other', database_url=url, database_schema=schema,
                  database_initialize=True, database_pool_size=1)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with other.connect() as conn:
                conn.begin_write()
                conn.execute('UPDATE runs SET summary=? WHERE id=?', ('Uncommitted', caller['id']))
                # The schema writer is intentionally still held when get must
                # finish. This also exercises its real read-only transaction.
                future = executor.submit(service.exchange, caller, {'action': 'get'}, internal=internal)
                result = future.result(timeout=2)
                assert result['inputs'][0]['content'] == 'Work until the checks pass.'
    finally:
        other.close()
