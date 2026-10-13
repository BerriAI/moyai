"""Slow blob transport must not retain a pool slot or writer admission."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
from uuid import uuid4

from fastapi import HTTPException
import pytest

from app.db import Store
from app.published_artifacts import PublishedArtifacts
from app.session_journal import SessionJournal
from storage_fixture import MemoryObjects
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app  # noqa: F401


@pytest.fixture
def storage(tmp_path):
    store = Store(tmp_path, database_pool_size=1)
    store.objects = MemoryObjects()
    run = store.create_run('Verify complete publication.', '', 'demo', [], chat_enabled=True, user_id='owner')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='test-capability')
    caller = store.run(run['id'])
    peer = store.create_run('Unrelated session', '', 'demo', [])
    journal = SessionJournal(store)
    journal.exchange(caller, {'action': 'import', 'manifest': {'id': 'empty', 'count': 0},
                              'entries': [], 'complete': True})
    artifacts = PublishedArtifacts(store)
    try:
        yield store, caller, peer, journal, artifacts
    finally:
        store.close()


def artifact_request(service, caller, *, commit):
    raw, upload = b'complete artifact bytes', uuid4().hex
    service.transfer(caller, {'action': 'begin', 'upload_id': upload, 'path': 'result.txt',
                              'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    part = {'action': 'part', 'upload_id': upload, 'part': 0,
            'data': base64.b64encode(raw).decode()}
    if commit:
        service.transfer(caller, part)
        return lambda: service.transfer(caller, {'action': 'commit', 'upload_id': upload, 'parts': 1}), 'read'
    return lambda: service.transfer(caller, part), 'put'


def blocked_storage(objects, method, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = getattr(objects, method)

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(objects, method, blocked)
    return entered, release


@pytest.mark.parametrize('operation', ['artifact_part', 'artifact_commit', 'journal_append', 'journal_read'])
def test_other_session_writer_completes_while_storage_is_pending(storage, monkeypatch, operation):
    store, caller, peer, journal, artifacts = storage
    entry = {'id': 'one', 'message': {'role': 'assistant', 'content': 'Verified result'}}
    if operation.startswith('artifact'):
        pending, method = artifact_request(artifacts, caller, commit=operation == 'artifact_commit')
    elif operation == 'journal_append':
        pending, method = lambda: journal.exchange(caller, {'action': 'append', 'entries': [entry]}), 'put'
    else:
        journal.exchange(caller, {'action': 'append', 'entries': [entry]})
        pending, method = lambda: journal.exchange(caller, {'action': 'read'}), 'read'
    entered, release = blocked_storage(store.objects, method, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer = executor.submit(pending)
        try:
            assert entered.wait(2)
            # Unscoped on purpose: this requires the real schema writer lock.
            writer = executor.submit(store.execute, 'UPDATE runs SET summary=? WHERE id=?',
                                     ('peer completed', peer['id']))
            assert writer.result(timeout=1) == 1
            assert not release.is_set() and not transfer.done()
        finally:
            release.set()
        assert transfer.result(timeout=2)
    assert store.run(peer['id'])['summary'] == 'peer completed'


@pytest.mark.parametrize('operation', ['artifact_part', 'artifact_commit', 'journal_append', 'journal_read'])
def test_authority_is_rechecked_after_storage_without_publishing_stale_data(storage, monkeypatch, operation):
    store, caller, _, journal, artifacts = storage
    entry = {'id': 'one', 'message': {'role': 'assistant', 'content': 'Old authority'}}
    if operation.startswith('artifact'):
        pending, method = artifact_request(artifacts, caller, commit=operation == 'artifact_commit')
    elif operation == 'journal_append':
        pending, method = lambda: journal.exchange(caller, {'action': 'append', 'entries': [entry]}), 'put'
    else:
        journal.exchange(caller, {'action': 'append', 'entries': [entry]})
        pending, method = lambda: journal.exchange(caller, {'action': 'read'}), 'read'
    entered, release = blocked_storage(store.objects, method, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer = executor.submit(pending)
        try:
            assert entered.wait(2)
            executor.submit(store.update_run, caller['id'], token_hash='replacement').result(timeout=1)
        finally:
            release.set()
        with pytest.raises(HTTPException) as denied:
            transfer.result(timeout=2)
        assert denied.value.status_code == 409
    assert not store.rows('SELECT * FROM published_artifacts')
    if operation == 'journal_append':
        assert not store.rows('SELECT * FROM session_items')


def test_artifact_commit_rechecks_the_exact_part_inventory(storage, monkeypatch):
    store, caller, _, _, artifacts = storage
    pending, method = artifact_request(artifacts, caller, commit=True)
    upload = store.rows('SELECT upload_id FROM artifact_uploads')[0]['upload_id']
    entered, release = blocked_storage(store.objects, method, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer = executor.submit(pending)
        try:
            assert entered.wait(2)
            executor.submit(artifacts.transfer, caller, {'action': 'part', 'upload_id': upload,
                'part': 1, 'data': base64.b64encode(b'extra').decode()}).result(timeout=1)
        finally:
            release.set()
        with pytest.raises(HTTPException, match='parts changed'):
            transfer.result(timeout=2)
    assert not store.rows('SELECT * FROM published_artifacts')


def test_journal_ack_retry_does_not_need_object_storage(storage):
    store, caller, _, journal, _ = storage
    body = {'action': 'append', 'entries': [
        {'id': 'receipt', 'message': {'role': 'assistant', 'content': 'Durably saved'}}]}
    first = journal.exchange(caller, body)
    store.objects.fail = True
    assert journal.exchange(caller, body) == first


def test_deepagents_publication_rechecks_steering_after_staging(active, monkeypatch):
    manager, store, scope = active
    store.objects = MemoryObjects()
    entered, release = blocked_storage(store.objects, 'put', monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(manager.deepagents_runtime.publish_text,
                                      scope, 'answer', 'Obsolete answer', scope.message_id)
        try:
            assert entered.wait(2)
            executor.submit(store.enqueue_message, scope.run_id, 'Correct the request', 'correction',
                            user_id='owner', send_now=True).result(timeout=1)
        finally:
            release.set()
        publication.result(timeout=2)
    assert not store.rows('SELECT * FROM session_items WHERE run_id=?', (scope.run_id,))
    assert not [event for event in store.events(scope.run_id) if event['kind'] == 'message']


@pytest.mark.parametrize('transport', ['append', 'commit_upload'])
def test_journal_reset_during_staging_cannot_restore_old_entries(storage, monkeypatch, transport):
    store, caller, _, journal, _ = storage
    body = {'action': 'append', 'entries': [
        {'id': 'old-epoch', 'message': {'role': 'assistant', 'content': 'Discard on reset'}}]}
    if transport == 'commit_upload':
        raw = json.dumps(body).encode()
        journal.exchange(caller, {'action': 'upload', 'id': 'reset-upload', 'part': 0,
                                  'data': base64.b64encode(raw).decode()})
        body = {'action': 'commit_upload', 'id': 'reset-upload', 'parts': 1,
                'sha256': hashlib.sha256(raw).hexdigest()}
    entered, release = blocked_storage(store.objects, 'put', monkeypatch)

    def reset():
        with store.connect(write_scope=caller['id']) as conn:
            conn.begin_write()
            for table in ('session_items', 'session_uploads', 'session_upload_receipts'):
                conn.execute(f'DELETE FROM {table} WHERE run_id=?', (caller['id'],))
            conn.execute('UPDATE session_journals SET epoch=? WHERE run_id=?',
                         (uuid4().hex, caller['id']))

    with ThreadPoolExecutor(max_workers=2) as executor:
        publication = executor.submit(journal.exchange, caller, body)
        try:
            assert entered.wait(2)
            executor.submit(reset).result(timeout=1)
        finally:
            release.set()
        with pytest.raises(HTTPException) as rejected:
            publication.result(timeout=2)
        assert rejected.value.status_code == 409
    assert not store.rows('SELECT * FROM session_items')


@pytest.mark.parametrize('accepted', [False, True])
def test_superseded_publication_does_not_need_object_storage(active, accepted):
    manager, store, scope = active
    store.objects = MemoryObjects()
    message, _ = store.enqueue_message(scope.run_id, 'Correction', 'steering-before-stage',
                                       user_id='owner', send_immediately=True)
    if accepted:
        control = manager.message_queue.live_control(scope.run_id, scope.message_id, [])
        assert control['input']['id'] == message['id']
        manager.message_queue.acknowledge(scope.run_id, scope.message_id, [message['id']])
    store.objects.fail = True
    manager.deepagents_runtime.publish_text(scope, 'obsolete', 'Discard this answer', scope.message_id)
    assert not store.rows('SELECT * FROM session_items WHERE run_id=?', (scope.run_id,))
    assert not [event for event in store.events(scope.run_id) if event['kind'] == 'message']


@pytest.mark.parametrize('existing', [False, True])
def test_original_input_storage_does_not_hold_writer_admission(active, monkeypatch, existing):
    manager, store, scope = active
    runtime = manager.deepagents_runtime
    store.objects = MemoryObjects()
    spec = {'prompt': 'Retain this exact original input.'}
    if existing:
        assert runtime.original_input(scope, spec) == spec['prompt']
    peer = store.create_run('Unrelated writer', '', 'demo', [])
    entered, release = blocked_storage(store.objects, 'read' if existing else 'put', monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        original = executor.submit(runtime.original_input, scope, spec)
        try:
            assert entered.wait(2)
            assert executor.submit(store.execute, 'UPDATE runs SET summary=? WHERE id=?',
                                   ('committed', peer['id'])).result(timeout=1) == 1
            assert not original.done()
        finally:
            release.set()
        assert original.result(timeout=2) == spec['prompt']
    assert store.run(peer['id'])['summary'] == 'committed'


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('changed', ['segment', 'epoch'])
def test_original_input_rechecks_execution_and_epoch_after_storage(active, monkeypatch, existing, changed):
    from temporalio.exceptions import ApplicationError
    manager, store, scope = active
    runtime = manager.deepagents_runtime
    store.objects = MemoryObjects()
    spec = {'prompt': 'Keep the authoritative input.'}
    if existing:
        runtime.original_input(scope, spec)
    entered, release = blocked_storage(store.objects, 'read' if existing else 'put', monkeypatch)

    def invalidate():
        if changed == 'segment':
            manager.save(scope.run_id, {**manager.state(scope.run_id), 'segment': 1})
        else:
            with store.connect(write_scope=scope.run_id) as conn:
                conn.begin_write()
                conn.execute('DELETE FROM session_items WHERE run_id=?', (scope.run_id,))
                conn.execute('UPDATE session_journals SET epoch=? WHERE run_id=?', (uuid4().hex, scope.run_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        original = executor.submit(runtime.original_input, scope, spec)
        try:
            assert entered.wait(2)
            executor.submit(invalidate).result(timeout=1)
        finally:
            release.set()
        with pytest.raises(ApplicationError if changed == 'segment' else HTTPException):
            original.result(timeout=2)
    items = store.rows('SELECT * FROM session_items WHERE run_id=?', (scope.run_id,))
    assert len(items) == int(existing and changed == 'segment')
