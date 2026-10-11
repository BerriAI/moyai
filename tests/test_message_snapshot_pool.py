"""Chat refreshes must not borrow a second connection inside their snapshot."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.db import Store


TEAM = 'T12345678'
TARGETS = ('U11111111', 'U22222222', 'U33333333', 'U44444444')


def test_parallel_message_snapshots_fit_in_the_existing_pool(tmp_path):
    store = Store(tmp_path, database_pool_size=8)
    if not store.database:
        store.close()
        pytest.skip('Pool contention requires the real PostgreSQL backend.')
    # Bound a regression failure without changing the production pool timeout.
    store.database.pool.timeout = .5
    run = store.create_run('Concurrent detail refresh', '', 'demo', [], chat_enabled=True)
    admitted = threading.Barrier(8)

    def refresh(_index):
        with store.connect() as connection:
            connection.begin_read()
            admitted.wait(5)  # Every slot is held before message decoration starts.
            return store.messages(run['id'], connection=connection)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(refresh, range(8)))
        assert all(rows[0]['content'] == 'Concurrent detail refresh' for rows in results)
        assert all(rows[0]['attachments'] == [] for rows in results)
        assert store.rows('SELECT 1 AS ok') == [{'ok': 1}]
    finally:
        store.close()


def test_readonly_snapshot_reuses_one_slot_and_backfills_after_release(tmp_path, monkeypatch):
    store = Store(tmp_path, database_pool_size=1)
    text = ' '.join(f'<@{target}>' for target in TARGETS)
    run = store.create_slack_run('EvSnapshot', text, [], 'C12345678', '1790719000.123456',
                                 'U87654321', team_id=TEAM)
    attachment = store.attachments.save('a' * 32, run['owner_id'], 'snapshot.txt', b'snapshot proof',
                                        ('text/plain', b'', 'snapshot proof'), 1024)
    with store.connect() as connection:
        message_id = connection.execute('SELECT id FROM messages WHERE run_id=?', (run['id'],)).fetchone()[0]
        store.attachments.bind_in(connection, [attachment['id']], message_id, run['owner_id'])
        profile = store.slack_identity_in(connection, TEAM, TARGETS[0])
        email = store.slack_identity_in(connection, TEAM, TARGETS[2])
    store.execute('UPDATE users SET name=?,email=? WHERE id=?', ('Profile name', 'profile@example.com', profile))
    store.execute('UPDATE users SET name=?,email=? WHERE id=?', ('', 'mail@example.com', email))
    store.execute('DELETE FROM slack_message_mentions')
    store.execute('DELETE FROM slack_mention_names')
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', (TEAM, TARGETS[0], 'Cached profile', 0))
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', (TEAM, TARGETS[1], 'Cached name', 0))
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', ('T87654321', TARGETS[3], 'Wrong workspace', 0))

    def no_nested_connection(*_args, **_kwargs):
        raise AssertionError('A message snapshot tried to borrow another connection')

    try:
        with store.connect() as connection:
            connection.begin_read()
            if store.database:
                assert connection.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
            with monkeypatch.context() as guard:
                guard.setattr(store, 'connect', no_nested_connection)
                rows = store.messages(run['id'], connection=connection)
            assert rows[0]['content'] == text
            assert rows[0]['display_content'] == '@Profile name @Cached name @mail@example.com <@U44444444>'
            assert rows[0]['attachments'] == [attachment]
            assert connection.execute('SELECT COUNT(*) FROM slack_message_mentions').fetchone()[0] == 0
            assert len(connection.commit_callbacks) == 1
        # The legacy discovery write runs only after the outer transaction/slot exits.
        mentions = store.rows('SELECT team_id,user_id FROM slack_message_mentions ORDER BY user_id')
        assert mentions == [{'team_id': TEAM, 'user_id': target} for target in TARGETS]
        assert store.messages(run['id'])[0]['display_content'] == rows[0]['display_content']
    finally:
        store.close()


def test_failed_snapshot_does_not_backfill_and_returns_its_pool_slot(tmp_path):
    store = Store(tmp_path, database_pool_size=1)
    run = store.create_slack_run('EvFailedSnapshot', f'<@{TARGETS[0]}>', [], 'C12345678',
                                 '1790719000.123456', 'U87654321', team_id=TEAM)
    store.execute('DELETE FROM slack_message_mentions')
    store.execute('DELETE FROM slack_mention_names')
    try:
        with pytest.raises(RuntimeError, match='abandoned snapshot'):
            with store.connect() as connection:
                connection.begin_read()
                store.messages(run['id'], connection=connection)
                raise RuntimeError('abandoned snapshot')
        assert store.rows('SELECT * FROM slack_message_mentions') == []
        assert store.rows('SELECT * FROM slack_mention_names') == []
    finally:
        store.close()
