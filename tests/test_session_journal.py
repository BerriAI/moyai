import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent.context_store import ContextUnavailable, open_context
from app.db import Store
from app.session_journal import SessionJournal


@pytest.fixture
def journal(tmp_path):
    store = Store(tmp_path / 'server')
    run = store.create_run('Task', '', 'demo', [], chat_enabled=True, user_id='owner')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='capability')
    caller = store.run(run['id'])
    service = SessionJournal(store)
    relay = SimpleNamespace(journal=lambda body: service.exchange(caller, body))
    yield store, caller, service, relay
    store.close()


def imported(relay):
    return relay.journal({'action': 'import', 'manifest': {'id': 'empty', 'count': 0}, 'entries': [], 'complete': True})


def test_computer_loss_recovers_complete_roles_and_receipts(journal, tmp_path):
    _, caller, _, relay = journal
    spec = {'run_id': caller['id']}
    first = open_context(tmp_path / 'machine1', spec, relay)
    first.append({'role': 'user', 'content': 'Keep every requirement ' + '界' * 30000})
    first.append({'role': 'assistant', 'tool_calls': [{'id': 'write', 'function': {'name': 'publish', 'arguments': '{}'}}]})
    first.append({'role': 'tool', 'tool_call_id': 'write', 'content': 'operation receipt'})
    checkpoint = first.checkpoint()
    original = [json.loads(r['message']) for r in first.db.execute('SELECT message FROM journal ORDER BY seq')]
    first.close()
    recovered = open_context(tmp_path / 'machine2', {**spec, 'context_checkpoint': checkpoint}, relay)
    assert recovered.checkpoint() == checkpoint
    assert [json.loads(r['message']) for r in recovered.db.execute('SELECT message FROM journal ORDER BY seq')] == original
    assert not recovered.pending
    recovered.close()


def test_incremental_hydration_preserves_summary_and_lost_ack_tail(journal, tmp_path):
    _, caller, _, relay = journal
    spec = {'run_id': caller['id']}
    context = open_context(tmp_path / 'machine', spec, relay)
    context.append({'role': 'user', 'content': 'original requirement'})
    context.compact(lambda *a, **k: 'Remember the original requirement', force=True)
    state = context.state()
    context.close()
    entry = {'id': 'stable', 'message': {'role': 'assistant', 'content': 'Saved after the computer checkpoint'}}
    a = relay.journal({'action': 'append', 'entries': [entry]})
    b = relay.journal({'action': 'append', 'entries': [entry]})
    assert a == b and b['seq'] == 2
    context = open_context(tmp_path / 'machine', spec, relay)
    assert context.state()['summary'] == state['summary']
    assert context.state()['cursor'] == state['cursor']
    assert context.checkpoint()['seq'] == 2
    context.close()
    with pytest.raises(HTTPException) as error:
        relay.journal({'action': 'append', 'entries': [{**entry, 'message': {'role': 'assistant', 'content': 'different'}}]})
    assert error.value.status_code == 409


def test_partial_legacy_import_cannot_be_blessed_by_missing_or_changed_checkpoint(journal, tmp_path):
    _, caller, _, relay = journal
    manifest = {'id': 'original', 'count': 2}
    first = {'id': 'legacy:1', 'message': {'role': 'user', 'content': 'Do not deploy'}}
    relay.journal({'action': 'import', 'manifest': manifest, 'entries': [first]})
    for request in [
        {'action': 'import', 'manifest': manifest, 'entries': [], 'complete': True},
        {'action': 'import', 'manifest': {'id': 'different', 'count': 0}, 'entries': [], 'complete': True},
        {'action': 'read'}, {'action': 'append', 'entries': [first]},
    ]:
        with pytest.raises(HTTPException) as error:
            relay.journal(request)
        assert error.value.status_code == 409
    with pytest.raises(ContextUnavailable, match='original conversation checkpoint'):
        open_context(tmp_path / 'lost', {'run_id': caller['id'], 'context_checkpoint': {'seq': 2}}, relay)
    relay.journal({'action': 'import', 'manifest': manifest, 'entries': [first,
        {'id': 'legacy:2', 'message': {'role': 'assistant', 'content': 'Understood'}}], 'complete': True})
    assert relay.journal({'action': 'read'})['seq'] == 2


@pytest.mark.parametrize('field,value', [('token_hash', 'renewed'), ('active_user_id', 'other'),
                                        ('active_message_id', 999), ('status', 'idle'), ('deleted_at', 'deleted')])
def test_stale_execution_cannot_read_or_append(journal, field, value):
    store, caller, _, relay = journal
    imported(relay)
    store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, caller['id']))
    with pytest.raises(HTTPException) as error:
        relay.journal({'action': 'read'})
    assert error.value.status_code == 409


def test_large_unicode_upload_is_complete_atomic_and_retry_safe(journal):
    store, caller, _, relay = journal
    imported(relay)
    message = {'role': 'tool', 'tool_call_id': 'report', 'content': '日志🔎' * 600000}
    raw = json.dumps({'action': 'append', 'entries': [{'id': 'big', 'message': message}]}, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    pieces = [raw[n:n+1000000] for n in range(0, len(raw), 1000000)]
    complete = {'action': 'commit_upload', 'id': digest, 'sha256': digest, 'parts': len(pieces)}
    with pytest.raises(HTTPException):
        relay.journal(complete)
    for n, chunk in enumerate(pieces):
        request = {'action': 'upload', 'id': digest, 'part': n, 'data': base64.b64encode(chunk).decode()}
        assert relay.journal(request) == relay.journal(request)
    assert relay.journal(complete)['seq'] == 1
    assert relay.journal(complete)['seq'] == 1  # Response lost after commit.
    assert relay.journal({'action': 'read'})['entries'][0]['message'] == message
    assert not store.rows('SELECT * FROM session_uploads')
    assert len(store.rows('SELECT * FROM session_upload_receipts')) == 1


def test_new_journal_scrubs_private_memory(journal):
    _, _, _, relay = journal
    imported(relay)
    relay.journal({'action': 'append', 'entries': [{'id': 'private', 'message': {
        'role': 'assistant', 'tool_calls': [{'id': 'secret', 'function': {
            'name': 'memory_save', 'arguments': '{"content":"PRIVATE CANARY"}'}}]}}]})
    assert 'PRIVATE CANARY' not in json.dumps(relay.journal({'action': 'read'}))


def test_no_session_turn_queue_or_event_history_caps(journal):
    store, caller, _, relay = journal
    for i in range(125):
        store.enqueue_message(caller['id'], 'Message ' + str(i), 'client-' + str(i), user_id='owner')
    assert len(store.messages(caller['id'])) == 126
    with store.connect() as conn:
        conn.executemany("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'tool','old','{}','')",
                         [(caller['id'],)] * 2001)
    text = 'full tool evidence' * 5000
    store.event(caller['id'], 'tool', text)
    assert store.rows('SELECT message FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1', (caller['id'],))[0]['message'] == text


def test_read_one_full_record_or_unicode_ranges_and_page_every_item(journal):
    _, _, _, relay = journal
    imported(relay)
    messages = [{'role': 'user', 'content': f'Item {i} ' + '日志🔎' * 3000} for i in range(12)]
    relay.journal({'action': 'append', 'entries': [{'id': str(i), 'message': message} for i, message in enumerate(messages)]})
    cursor, collected = 0, []
    while True:
        page = relay.journal({'action': 'read', 'after': cursor, 'limit': 3})
        collected.extend(row['message'] for row in page['entries'])
        if page['next'] is None:
            break
        cursor = page['next']
    assert collected == messages
    complete = relay.journal({'action': 'read', 'seq': 7})['entry']
    parts, offset = [], 0
    while True:
        entry = relay.journal({'action': 'read', 'seq': 7, 'offset': offset, 'characters': 137})['entry']
        assert entry['sha256'] == complete['sha256']
        parts.append(entry['text'])
        if entry['next_offset'] is None:
            break
        offset = entry['next_offset']
    assert ''.join(parts) == complete['text']
    assert json.loads(complete['text']) == messages[6]


def test_real_relay_envelope_chunks_large_item_and_recovers_lost_commit_ack(journal, monkeypatch):
    import io
    import urllib.error
    from sandbox import broker_relay, startup
    from sandbox.broker_transport import unseal, MAX_BODY
    _, caller, service, _ = journal
    relay = broker_relay.BrokerRelay.__new__(broker_relay.BrokerRelay)
    relay.remote, relay.token, relay.notify = 'https://broker.test', 'test-capability', lambda text: None
    actions, lost = [], []
    def open_request(request, **kwargs):
        assert request.get_method() == 'POST'
        body = json.loads(unseal(relay.token, '/context/journal', request.data))
        assert len(json.dumps(body, ensure_ascii=False).encode()) <= MAX_BODY
        actions.append(body['action'])
        result = service.exchange(caller, body)
        if body['action'] == 'commit_upload' and not lost:
            lost.append(True)
            raise urllib.error.URLError('Response lost after durable commit')
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(broker_relay, 'open_broker', open_request)
    monkeypatch.setattr(broker_relay, '_read_with_reconnect', lambda *a, **kw:
                        startup._read_with_reconnect(*a, **kw, sleep=lambda seconds: None))
    imported(relay)
    message = {'role': 'tool', 'tool_call_id': 'large', 'content': '日志🔎' * 600000}
    receipt = relay.journal({'action': 'append', 'entries': [{'id': 'relay-record', 'message': message}]})
    assert receipt['seq'] == 1
    assert actions.count('upload') > 1 and actions.count('commit_upload') == 2
    assert relay.journal({'action': 'read'})['entries'][0]['message'] == message


def test_slack_source_watermark_survives_computer_loss_without_repeating_old_context(journal, tmp_path):
    _, caller, _, relay = journal
    first = open_context(tmp_path / 'first', {'run_id': caller['id']}, relay)
    source = {'through_seq': 10, 'messages': [{'seq': 8, 'text': 'Old peer reply'}, {'seq': 10, 'text': 'Latest source'}]}
    assert len(first.source_view(source)['messages']) == 2
    first.append({'role': 'user', 'content': 'Current task with the complete source view: ' + json.dumps(source)})
    first.close()
    second = open_context(tmp_path / 'renewed', {'run_id': caller['id']}, relay)
    new_source = {**source, 'through_seq': 12, 'messages': [*source['messages'], {'seq': 12, 'text': 'Later edit'}]}
    assert second.source_view(new_source)['messages'] == [{'seq': 12, 'text': 'Later edit'}]
    assert 'Old peer reply' in json.dumps(second.history())
    second.close()
