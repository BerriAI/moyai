"""Slack's historical collection must yield the shared database to other work."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_slack import slack_app  # noqa: F401


def historical_threads(slack_app, count=8):
    app, client, _, _ = slack_app
    chat, store = app.state.slack.chat, app.state.store
    client.portal.call(chat.shutdown)
    runs = []
    for index in range(count):
        run = store.create_run('Historical Slack conversation', '', 'demo', [], chat_enabled=True)
        store.update_run(run['id'], status='idle')
        store.execute('''INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts)
            VALUES('T12345678','C12345678',?,?,'1790719000.123456')''',
            (f'17907190{index:02d}.123456', run['id']))
        runs.append(run['id'])
    return app, client, sorted(runs)


def test_historical_collection_releases_writer_between_threads(slack_app, monkeypatch, record_property):
    app, _, runs = historical_threads(slack_app)
    chat, store = app.state.slack.chat, app.state.store
    if not store.database:
        pytest.skip('The queued advisory-lock regression requires PostgreSQL.')
    entered, release, writer_started, writer_done = (threading.Event() for _ in range(4))
    collect_answers = chat.collect_answers_in
    visited, backend = [], {}

    def historical_work(conn, binding, allowed):
        visited.append(binding['run_id'])
        if len(visited) == 1:
            entered.set()
            assert release.wait(5)
        # Exercise an actual database operation while each thread is collected.
        # The baseline holds the global lock across all eight slow operations.
        conn.execute('SELECT pg_sleep(0.05)')
        collect_answers(conn, binding, allowed)

    def unrelated_writer():
        began = time.monotonic()
        with store.connect() as conn:
            backend['pid'] = conn.raw.info.backend_pid
            writer_started.set()
            conn.execute('UPDATE runs SET summary=? WHERE id=?', ('Unrelated write completed', runs[-1]))
        backend['elapsed_ms'] = round((time.monotonic() - began) * 1000, 2)
        writer_done.set()
        return len(visited)

    monkeypatch.setattr(chat, 'collect_answers_in', historical_work)
    with ThreadPoolExecutor(max_workers=2) as executor:
        collection = executor.submit(chat.collect)
        writer = None
        try:
            assert entered.wait(3)
            writer = executor.submit(unrelated_writer)
            assert writer_started.wait(3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if store.rows("SELECT 1 FROM pg_locks WHERE pid=? AND locktype='advisory' AND NOT granted",
                              (backend['pid'],)):
                    break
                time.sleep(.005)
            else:
                pytest.fail('The unrelated writer did not queue behind the collector lock.')
            release.set()
            completed_after = writer.result(timeout=3)
            record_property('historical_threads', len(runs))
            record_property('writer_completed_after_threads', completed_after)
            record_property('writer_elapsed_ms', backend['elapsed_ms'])
            assert completed_after < len(runs), 'The collector monopolized writes across all historical threads.'
            collection.result(timeout=5)
        finally:
            release.set()
            collection.result(timeout=5)
            if writer:
                writer.result(timeout=5)
    assert writer_done.is_set() and len(visited) == len(runs)
    assert store.run(runs[-1])['summary'] == 'Unrelated write completed'


@pytest.mark.parametrize('change', ['delete', 'disable', 'pause'])
def test_each_thread_revalidates_after_candidate_scan(slack_app, monkeypatch, change):
    app, _, runs = historical_threads(slack_app, count=2)
    chat, store = app.state.slack.chat, app.state.store
    collect_thread = chat.collect_thread
    seen = []

    def candidate_changed(run_id):
        if run_id == runs[-1]:
            if change == 'delete':
                store.execute('DELETE FROM slack_threads WHERE run_id=?', (run_id,))
            elif change == 'disable':
                app.state.settings.slack_thread_chat_enabled = False
            else:
                store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
        return collect_thread(run_id)

    def collect_answers(conn, binding, allowed):
        seen.append((binding['run_id'], bool(allowed), bool(binding['paused'])))

    monkeypatch.setattr(chat, 'collect_thread', candidate_changed)
    monkeypatch.setattr(chat, 'collect_answers_in', collect_answers)
    chat.collect()
    assert seen[0] == (runs[0], True, False)
    assert seen[1:] == ([] if change == 'delete' else [(runs[1], change != 'disable', change == 'pause')])


def test_failed_later_thread_does_not_replay_committed_collection(slack_app, monkeypatch):
    app, _, runs = historical_threads(slack_app, count=2)
    chat, store = app.state.slack.chat, app.state.store
    for run_id in runs:
        message = store.claim_message(run_id)
        store.finish_message(run_id, message['id'], 'Saved answer awaiting collection.')
        # Recreate a legacy unseen answer, without its already-atomic intent.
        store.execute('DELETE FROM slack_outbox WHERE run_id=?', (run_id,))
        store.execute('UPDATE slack_threads SET last_message_id=0 WHERE run_id=?', (run_id,))
    collect_answers = chat.collect_answers_in

    def failing_second(conn, binding, allowed):
        collect_answers(conn, binding, allowed)
        if binding['run_id'] == runs[-1]:
            raise RuntimeError('Second thread failed after its cursor and intent writes')

    monkeypatch.setattr(chat, 'collect_answers_in', failing_second)
    with pytest.raises(RuntimeError, match='Second thread failed'):
        chat.collect()
    rows = store.rows("SELECT run_id,status FROM slack_outbox WHERE kind='answer' ORDER BY run_id")
    assert rows == [{'run_id': runs[0], 'status': 'pending'}]
    assert store.rows('SELECT last_message_id FROM slack_threads WHERE run_id=?', (runs[-1],)) == [{'last_message_id': 0}]
    monkeypatch.setattr(chat, 'collect_answers_in', collect_answers)
    chat.collect()
    chat.collect()
    assert store.rows("SELECT run_id,status FROM slack_outbox WHERE kind='answer' ORDER BY run_id") == [
        {'run_id': run_id, 'status': 'pending'} for run_id in runs]
