import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from sandbox.context_store import ContextStore, ContextUnavailable, open_context, read_records, BATCH_BYTES, SUMMARY_BYTES
from sandbox.harness_agent import TurnJournal
from sandbox.continuation import RotationDeadline


def add_round(store, number, output='completed'):
    journal = TurnJournal([], f'Continue step {number}', store)
    journal.tool_started(f'call{number}', 'publish', {'operation': number})
    journal.tool_finished(f'call{number}', f'receipt-{number}: ' + output)
    return journal


def facts_summary(previous, entries):
    """Deterministic inference fixture; no claim about live summary quality."""
    text = previous + json.dumps(entries)
    facts = [fact for fact in ['Keep Escape support', 'Do not deploy', 'PR #127 created'] if fact in text]
    return '\n'.join(facts) + '\nCovered through record ' + str(entries[-1]['seq'])


def test_many_cold_checkpoints_read_only_new_previews_and_keep_receipts(tmp_path):
    path = tmp_path / 'live' / 'context.sqlite3'
    store = ContextStore(path, 'run')
    store.initialize([{'role': 'user', 'content': 'Keep Escape support. Do not deploy.'},
                      {'role': 'assistant', 'content': 'PR #127 created'}])
    batches, max_prompt = [], 0
    def summarize(previous, entries):
        assert len(json.dumps(entries, ensure_ascii=False).encode()) <= BATCH_BYTES
        assert len(previous.encode()) <= SUMMARY_BYTES
        batches.append([entry['seq'] for entry in entries])
        return facts_summary(previous, entries)
    for checkpoint in range(30):
        for i in range(20):
            add_round(store, checkpoint * 20 + i, 'large diagnostic output\n' * 600)
        store.compact(summarize)
        history = store.history()
        size = len(json.dumps(history).encode())
        max_prompt = max(max_prompt, size)
        assert size < 48_000
        assert all(fact in history[0]['content'] for fact in ['Keep Escape support', 'Do not deploy', 'PR #127 created'])
        cursor = store.state()['cursor']
        store.close()
        # Copy only the closed database, as a cold filesystem snapshot would.
        restored = tmp_path / f'checkpoint-{checkpoint}.sqlite3'
        shutil.copy2(path, restored)
        store = ContextStore(restored, 'run')
        path = restored
        assert store.state()['cursor'] == cursor
        assert not store.pending
    ids = [seq for batch in batches for seq in batch]
    assert ids == list(range(1, store.state()['cursor'] + 1))
    assert store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == 1802
    assert 'receipt-0' in read_records(path, after=4, limit=1)[0]['text']
    assert 'receipt-599' in read_records(path, after=1801, limit=1)[0]['text']
    assert max_prompt < 48_000
    store.close()


@pytest.mark.parametrize('failure', ['exception', 'empty', 'oversize', 'wrong_type'])
def test_summary_failure_retains_cursor_and_receipts_then_retries_delta(tmp_path, failure):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([])
    for i in range(50): add_round(store, i, 'data' * 1000)
    calls = []
    def summarize(previous, entries):
        calls.append(entries)
        if len(calls) == 1:
            return 'Last good summary'
        if failure == 'exception': raise TimeoutError('provider secret')
        return {'empty': '', 'oversize': 'x' * (SUMMARY_BYTES + 1), 'wrong_type': None}[failure]
    with pytest.raises(ContextUnavailable, match='Saved receipts are preserved'):
        store.compact(summarize)
    state = store.state()
    assert state['summary'] == 'Last good summary'
    assert state['cursor'] == calls[0][-1]['seq']
    assert store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == 150
    store.close()
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    retry = []
    store.compact(lambda previous, entries: retry.append(entries) or facts_summary(previous, entries))
    assert retry[0] == calls[1]
    store.close()


def test_crash_during_summary_commit_rolls_back_both_summary_and_cursor(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([])
    for i in range(50): add_round(store, i)
    store.db.execute("CREATE TRIGGER crash BEFORE UPDATE OF cursor ON state BEGIN SELECT RAISE(ABORT, 'crash'); END")
    with pytest.raises(sqlite3.IntegrityError): store.compact(facts_summary)
    store.close()
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    assert store.state()['cursor'] == 0 and store.state()['summary'] == ''
    store.db.execute('DROP TRIGGER crash')
    store.compact(facts_summary)
    assert store.state()['cursor'] > 0
    store.close()


def test_parallel_tools_and_crash_with_unknown_outcome_never_authorize_resume(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([])
    journal = TurnJournal([], 'Task', store)
    for call in ['a', 'b']: journal.tool_started(call, 'publish', {})
    journal.tool_finished('a', 'receipt')
    deadline = RotationDeadline(1)
    deadline.requested = True
    assert not deadline.can_continue({'interrupted': True, 'messages': journal.messages})
    store.close()
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    assert store.pending == {'b'}
    with pytest.raises(ContextUnavailable, match='unfinished tools'):
        store.compact(lambda *_: pytest.fail('Must not call inference'))
    # Only an actual receipt settles the pending operation.
    store.append({'role': 'tool', 'tool_call_id': 'b', 'content': 'verified receipt'})
    assert not store.pending
    store.close()


def test_legacy_migration_is_once_scrubbed_and_scoped(tmp_path, monkeypatch):
    history = [{'role': 'assistant', 'tool_calls': [{'id': 'private', 'function': {
        'name': 'mcp__moyai__memory_save', 'arguments': '{"content":"PRIVATE MARKER"}'}}]},
        {'role': 'tool', 'tool_call_id': 'private', 'content': 'saved'}]
    (tmp_path / 'conversation.json').write_text(json.dumps(history))
    store = open_context(tmp_path, {'run_id': 'run'})
    assert 'PRIVATE MARKER' not in json.dumps(read_records(store.path))
    store.close()
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: pytest.fail('Steady state must not load legacy JSON'))
    store = open_context(tmp_path, {'run_id': 'run', 'history_fallback': [{'role': 'user', 'content': 'duplicate'}]})
    assert len(read_records(store.path)) == 2
    store.close()
    with pytest.raises(ContextUnavailable, match='different session'):
        open_context(tmp_path, {'run_id': 'unrelated'})


@pytest.mark.parametrize('flag', ['fresh_child', 'workspace_warning'])
def test_inherited_and_stale_context_reset_to_canonical_fallback(tmp_path, flag):
    store = ContextStore(tmp_path / 'context.sqlite3', 'old')
    store.initialize([{'role': 'user', 'content': 'old context'}])
    store.close()
    (tmp_path / 'conversation.json').write_text(json.dumps([{'role': 'user', 'content': 'old legacy'}]))
    store = open_context(tmp_path, {'run_id': 'new', flag: True,
        'history_fallback': [{'role': 'user', 'content': 'new context'}]})
    assert 'new context' in store.history()[0]['content']
    assert 'old context' not in store.history()[0]['content']
    assert 'old legacy' not in store.history()[0]['content']
    store.close()


def test_archive_reads_page_large_unicode_receipts_and_use_no_full_blob_query(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    raw = '日志🔎' * 200_000
    store.initialize([{'role': 'assistant', 'content': raw}])
    queries = []
    store.db.set_trace_callback(queries.append)
    store.history()
    assert not any('select' in q.lower() and 'message' in q.lower() for q in queries)
    records = read_records(store.path, limit=1, offset=5000)
    expected = json.dumps({'role': 'assistant', 'content': raw}, ensure_ascii=False)
    assert records[0]['text'] == expected[5000:9000]
    assert records[0]['characters'] == len(expected)
    store.close()


def test_migration_transaction_rolls_back_and_corrupt_store_is_not_discarded(tmp_path):
    path = tmp_path / 'context.sqlite3'
    store = ContextStore(path, 'run')
    with pytest.raises(AttributeError): store.initialize([{'role': 'user', 'content': 'valid'}, None])
    assert not store.has_history and not store.state()['initialized']
    store.close()
    path.write_bytes(b'corrupted checkpoint')
    with pytest.raises(sqlite3.DatabaseError): ContextStore(path, 'run')
    assert path.read_bytes() == b'corrupted checkpoint'


def test_invalid_coverage_cursor_cannot_silently_hide_history(tmp_path):
    path = tmp_path / 'context.sqlite3'
    store = ContextStore(path, 'run')
    store.initialize([{'role': 'user', 'content': 'Do not deploy'}])
    with store.db:
        store.db.execute("UPDATE state SET cursor=500, summary='bad state'")
    store.close()
    with pytest.raises(ContextUnavailable, match='invalid summary position'):
        ContextStore(path, 'run')
    assert 'Do not deploy' in read_records(path)[0]['text']
