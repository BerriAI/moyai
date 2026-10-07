import json
import base64
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from sandbox.context_store import ContextStore, ContextUnavailable, open_context, read_records, BATCH_BYTES, SUMMARY_BYTES
from sandbox.harness_agent import TurnJournal
from sandbox.continuation import RotationDeadline


@pytest.fixture
def native_checkpoint(tmp_path):
    from app.native_sessions import NativeSessions
    from sandbox.native_session import NativeSession
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'content': 'Public completed receipt'}])
    requests = []
    def exchange(body):
        # Exercise the production server contract, not a permissive fake that
        # would accept a commit the real encrypted endpoint rejects.
        NativeSessions.validate(None, body)
        requests.append(body)
        return {'lease': body['lease'], 'state': None, 'reason': 'missing'}
    context = SimpleNamespace(cwd=str(tmp_path), spec={}, relay=SimpleNamespace(native=exchange))
    native = NativeSession(context, store, 'codex', {'adapter': 1})
    native.begin()
    try:
        yield native, store, requests
    finally:
        native.close()
        store.close()


@pytest.mark.parametrize('change', [None, 'journal_append', 'journal_reset', 'unresolved_tool'])
def test_native_commit_requires_the_exact_completed_public_checkpoint(native_checkpoint, change):
    native, store, requests = native_checkpoint
    payload = {'opaque': ['PRIVATE NATIVE MARKER']}
    native.finish(payload)
    payload['opaque'].append('Changed after staging')
    marker = native.root / 'claude/private.jsonl'
    marker.write_text('PRIVATE NATIVE MARKER')
    if change == 'journal_append':
        store.append({'role': 'user', 'content': 'Later message'})
    elif change == 'journal_reset':
        with store.db:
            store.db.execute("UPDATE state SET epoch=?", ('b' * 32,))
    elif change == 'unresolved_tool':
        TurnJournal([], 'Inspect', store).tool_started('unknown', 'Edit', {})
    native.close()
    commits = [request for request in requests if request['action'] == 'commit']
    assert len(commits) == (0 if change else 1)
    if commits:
        assert commits[0]['compatibility'] == native.compatibility
        assert commits[0]['state'] == {'opaque': ['PRIVATE NATIVE MARKER']}
        assert commits[0]['checkpoint'] == native.checkpoint()
    assert not marker.exists() and not native.root.exists()
    assert 'PRIVATE NATIVE MARKER' not in json.dumps(read_records(store.path))


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'oversize', 'wrong_lease', 'unavailable',
                                     'restart_unavailable', 'restart_wrong_lease'])
def test_missing_or_corrupt_native_load_preserves_the_public_fallback(native_checkpoint, failure):
    from sandbox.native_session import MAX_BYTES
    native, store, requests = native_checkpoint
    private = native.root / 'claude/previous.jsonl'
    private.write_text('OLD PRIVATE STATE')
    def load(body):
        if failure == 'unavailable' or (failure == 'restart_unavailable' and body['action'] == 'restart'):
            raise OSError('Native service unavailable')
        state = {'private': 'OLD PRIVATE STATE'}
        if failure == 'missing': state = None
        if failure == 'corrupt' or failure.startswith('restart_'): state = ['bad state']
        if failure == 'oversize': state = {'large': 'x' * (MAX_BYTES + 1)}
        wrong_lease = failure == 'wrong_lease' or (failure == 'restart_wrong_lease' and body['action'] == 'restart')
        return {'lease': 'different-lease' if wrong_lease else body['lease'], 'state': state}
    native.context.relay.native = load
    assert native.begin() is None and not native.resumed
    if failure in {'wrong_lease', 'unavailable', 'restart_unavailable', 'restart_wrong_lease'}:
        assert not native.enabled
        native.finish({'opaque': 'Unacknowledged replacement'})
        assert native.staged is None and not any(body['action'] == 'commit' for body in requests)
    assert not private.exists()
    assert 'Public completed receipt' in store.history()[0]['content']
    assert 'OLD PRIVATE STATE' not in json.dumps(read_records(store.path))


@pytest.mark.parametrize('reason', ['pending', 'fresh_child', 'workspace_warning'])
def test_unconfirmed_or_inherited_native_state_is_invalidated(native_checkpoint, reason):
    native, store, requests = native_checkpoint
    exchange = native.context.relay.native
    def load(body):
        reply = exchange(body)
        if body['action'] == 'begin': reply['state'] = {'private': 'PARENT STATE'}
        return reply
    native.context.relay.native = load
    if reason == 'pending':
        TurnJournal([], 'Check earlier work', store).tool_started('uncertain', 'Edit', {})
    else:
        native.context.spec[reason] = True
    assert native.begin() is None and not native.resumed
    assert requests[-1]['action'] == 'invalidate'
    assert 'PARENT STATE' not in json.dumps(read_records(store.path))


@pytest.mark.parametrize('bad_name', ['../escape', '/absolute', 'nested/../../escape', 'windows\\escape', 'nul\x00name'])
def test_native_file_restore_rejects_bad_names_before_writing(native_checkpoint, bad_name):
    native, _, _ = native_checkpoint
    data = base64.b64encode(b'private').decode()
    with pytest.raises(ValueError):
        native.restore_files(native.cache, {'valid.json': data, bad_name: data})
    assert not (native.cache / 'valid.json').exists()


def test_native_file_roundtrip_is_private_and_size_count_bounded(native_checkpoint, monkeypatch):
    from sandbox import native_session
    native, _, _ = native_checkpoint
    data = base64.b64encode(b'private').decode()
    native.restore_files(native.cache, {'nested/session.json': data})
    target = native.cache / 'nested/session.json'
    assert target.read_bytes() == b'private' and target.stat().st_mode & 0o777 == 0o600
    assert native.files(native.cache) == {'nested/session.json': data}
    for snapshot in ({'valid': data, 'bad': 'not base64!'}, {'valid': data, 'large': base64.b64encode(b'x' * 33).decode()}):
        monkeypatch.setattr(native_session, 'MAX_BYTES', 32)
        with pytest.raises(ValueError):
            native.restore_files(native.root / 'restore', snapshot)
        assert not (native.root / 'restore/valid').exists()
    monkeypatch.setattr(native_session, 'MAX_FILES', 1)
    with pytest.raises(ValueError):
        native.restore_files(native.root / 'restore', {'one': data, 'two': data})
    (target.parent / 'second').write_text('second')
    with pytest.raises(ValueError):
        native.files(native.cache)


@pytest.mark.parametrize('destination', ['outside', 'inside'])
def test_native_file_restore_and_snapshot_reject_ancestor_symlinks(native_checkpoint, tmp_path, destination):
    native, _, _ = native_checkpoint
    target = tmp_path / 'outside' if destination == 'outside' else native.root / 'owned'
    (target / 'nested').mkdir(parents=True)
    secret = target / 'nested/private.json'
    secret.write_text('PRIVATE DO NOT COPY')
    (native.root / 'link').symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        native.files(native.root / 'link/nested')
    encoded = base64.b64encode(b'overwrite').decode()
    with pytest.raises(ValueError):
        native.restore_files(native.root, {'valid.json': encoded, 'link/nested/private.json': encoded})
    assert not (native.root / 'valid.json').exists()
    assert secret.read_text() == 'PRIVATE DO NOT COPY'


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


def test_interrupted_tools_allow_investigation_without_settling_or_replaying(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([])
    journal = TurnJournal([], 'Task', store)
    for call in ['a', 'b']: journal.tool_started(call, 'publish', {})
    journal.tool_finished('a', 'receipt')
    unresolved = store.pending
    assert len(unresolved) == 1
    deadline = RotationDeadline(1)
    deadline.requested = True
    assert not deadline.can_continue({'interrupted': True, 'messages': journal.messages})
    store.close()
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    assert store.pending == unresolved
    for i in range(50): store.append({'role': 'assistant', 'content': f'Later record {i}'})
    store.compact(lambda *_: 'Summary deliberately omits the interrupted tool.')
    assert store.state()['cursor'] > 3
    history = store.history()[0]['content']
    assert 'UNRESOLVED TOOL OUTCOMES' in history and next(iter(unresolved)) in history
    assert 'unknown' in history and 'before repeating' in history
    assert store.pending == unresolved
    assert store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == 54
    assert 'publish' in read_records(store.path, after=2, limit=1)[0]['text']
    # Only an actual receipt settles the pending operation.
    store.append({'role': 'tool', 'tool_call_id': next(iter(unresolved)), 'content': 'verified receipt'})
    assert not store.pending
    assert 'UNRESOLVED TOOL OUTCOMES' not in store.history()[0]['content']
    store.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_fresh_runtime_reusing_tool_id_cannot_settle_interrupted_call(tmp_path, legacy):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([])
    if legacy:
        store.append({'role': 'assistant', 'tool_calls': [{'id': 'item_0', 'function': {'name': 'Edit'}}]})
    else:
        TurnJournal([], 'Edit the file', store).tool_started('item_0', 'Edit', {})
    unresolved = store.pending
    recovered = TurnJournal(store.history(), 'Inspect the file', store)
    recovered.tool_started('item_0', 'Read', {})
    assert len(store.pending) == 2
    recovered.tool_finished('item_0', 'file contents')
    assert store.pending == unresolved and not recovered.pending
    assert recovered.messages[-1]['tool_call_id'] == 'item_0'
    store.close()


def test_unresolved_notice_stays_bounded_with_many_large_ids(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'user', 'content': 'Investigate'}])
    with store.db:
        store.db.executemany('INSERT INTO pending VALUES(?)', [(f'{i}-' + '界' * 1000,) for i in range(100)])
    history = store.history()[0]['content']
    assert '100' in history and 'UNRESOLVED TOOL OUTCOMES' in history
    assert len(history.encode()) < 5_000
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


def test_full_tail_and_budget_trigger_ignore_receipt_count(tmp_path):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'content': f'receipt-{i:03d}'} for i in range(90)])
    assert all(f'receipt-{i:03d}' in store.history()[0]['content'] for i in range(90))
    assert store.maintenance_snapshot(100_000) is None
    snapshot = store.maintenance_snapshot(8_000)
    assert snapshot and snapshot['cursor'] == 0
    store.append({'role': 'user', 'content': 'New correction: never deploy'})
    assert store.apply_summary(snapshot, {'summary': 'Original receipts retained', 'through_seq': snapshot['entries'][-1]['seq']})
    assert 'never deploy' in store.history()[0]['content'] and 'receipt-089' in store.history()[0]['content']
    assert not store.apply_summary(snapshot, 'stale summary')
    # Resetting the same run may reproduce exactly the same prefix. Its epoch
    # must still prevent a late summary from the discarded checkpoint applying.
    old_epoch = store.state()['epoch']
    store.close()
    reset = open_context(tmp_path, {'run_id': 'run', 'workspace_warning': True,
        'history_fallback': [{'role': 'assistant', 'content': f'receipt-{i:03d}'} for i in range(90)]})
    assert reset.state()['epoch'] != old_epoch and not reset.apply_summary(snapshot, 'wrong generation')
    reset.close()


@pytest.mark.parametrize('recent_rows,budget', [(1, 8_000), (2, 16_000), (8, 16_000), (12, 16_000)])
@pytest.mark.parametrize('summarized', [False, True])
def test_large_recent_tail_can_be_summarized_in_background(tmp_path, recent_rows, budget, summarized):
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'content': 'Older completed work'}] if summarized else [])
    if summarized:
        store.compact(lambda *a, **k: 'Earlier receipts', force=True)
    for i in range(recent_rows):
        store.append({'role': 'user', 'content': f'Correction {i}: ' + 'detail ' * 1200})
    original = list(store.db.execute('SELECT seq,message FROM journal ORDER BY seq'))
    cursor = store.state()['cursor']
    snapshot = store.maintenance_snapshot(budget)
    assert snapshot is not None  # Even two large records require maintenance.
    for _ in range(recent_rows):
        if snapshot is None:
            break
        assert snapshot['entries'][0]['seq'] == cursor + 1
        assert len(json.dumps(snapshot['entries'], ensure_ascii=False).encode()) <= BATCH_BYTES + 2
        assert store.apply_summary(snapshot, 'Keep the corrections; original receipts remain available.')
        assert store.state()['cursor'] > cursor
        cursor = store.state()['cursor']
        snapshot = store.maintenance_snapshot(budget)
    assert snapshot is None
    tail = store.db.execute('SELECT seq,preview FROM journal WHERE seq>?', (cursor,)).fetchall()
    assert tail or recent_rows == 1
    if tail:
        assert f'Correction {recent_rows - 1}' in tail[-1]['preview']
    assert len(store.state()['summary'].encode()) + len(json.dumps([
        {'seq': row['seq'], 'excerpt': row['preview']} for row in tail]).encode()) < budget * .6
    assert list(store.db.execute('SELECT seq,message FROM journal ORDER BY seq')) == original
    store.close()


def test_summary_network_call_does_not_lock_append_and_stale_result_loses(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store = ContextStore(tmp_path / 'context.sqlite3', 'run')
    store.initialize([{'role': 'assistant', 'content': f'receipt-{i}'} for i in range(60)])
    entered, release = threading.Event(), threading.Event()
    def summarize(previous, entries):
        entered.set()
        assert release.wait(3)
        return 'slow stale summary'
    with ThreadPoolExecutor(2) as pool:
        work = pool.submit(store.compact, summarize)
        try:
            assert entered.wait(2)
            pool.submit(store.append, {'role': 'user', 'content': 'New correction'}).result(timeout=1)
            assert 'New correction' in store.history()[0]['content']
            # A mandatory recovery wins while optional work is still in flight.
            store.compact(lambda *a, **k: 'Newer forced summary', force=True)
        finally:
            release.set()
        work.result(timeout=2)
    assert store.state()['summary'] == 'Newer forced summary'
    store.close()


def test_database_cas_rejects_another_connections_newer_summary(tmp_path, monkeypatch):
    path = tmp_path / 'context.sqlite3'
    first = ContextStore(path, 'run')
    first.initialize([{'role': 'assistant', 'content': f'receipt-{i}'} for i in range(40)])
    second = ContextStore(path, 'run')
    snapshot = first.snapshot()
    read = first.snapshot
    def concurrent_commit(through):
        observed = read(through)
        assert second.apply_summary(snapshot, 'Newer committed result')
        return observed
    monkeypatch.setattr(first, 'snapshot', concurrent_commit)
    try:
        assert not first.apply_summary(snapshot, 'Stale competing result')
        assert first.state()['summary'] == 'Newer committed result'
    finally:
        first.close()
        second.close()
