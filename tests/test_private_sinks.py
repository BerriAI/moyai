"""Internal sink tests deliberately bypass the central HTTP ACL.

Real HTTP handlers, signed individual sessions, SQLite and stored bytes; no
network, model or Slack calls.
"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.config import Settings
from app import artifact_files, captures
from app.agents import Fanout
from app.trace_outbox import TraceOutbox, RaindropEventOutbox
from app.tracing import AgentTracing
from app.automation_events import AutomationEvents
from app.security import digest

SECRET = 'private-slack-sentinel-cobalt-8372'
PNG = b'\x89PNG\r\n\x1a\n' + SECRET.encode()


@pytest.fixture
def sinks(tmp_path):
    values = {k: f.get_default(call_default_factory=True) for k, f in Settings.model_fields.items()}
    values.update(data_dir=tmp_path, public_url='http://127.0.0.1:8787',
                  google_client_id='synthetic-google', google_client_secret='synthetic-secret',
                  google_admin_emails='admin@berri.ai', session_secret='test-private-session',
                  session_titles_enabled=False, memory_review_enabled=True,
                  litellm_api_key='', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    state = app.state
    state.store.identity({'method': 'google', 'identity': {'sub': 'alice', 'email': 'alice@berri.ai'}})
    state.security.session_privacy = None
    # Include only sink routers: a main middleware fix cannot make these pass.
    http = FastAPI()
    http.include_router(artifact_files.routes(state.settings, state.store, state.security))
    http.include_router(state.computer.routes())
    http.include_router(state.store.attachments.routes(state.security, state.settings))
    http.include_router(state.media_shares.routes())
    client = TestClient(http, base_url=state.settings.public_url)
    yield state, client
    client.close()


def login(state, client, user):
    response = JSONResponse({})
    sid = state.security.new_session(response, identity={'sub': user, 'email': user+'@berri.ai',
        'domain': 'berri.ai', 'name': user.title()})
    state.store.identity({'method': 'google', 'identity': {'sub': user, 'email': user+'@berri.ai'}})
    client.cookies.set('workspace_session', response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0])
    client.headers.update({'X-CSRF-Token': state.security.csrf(sid), 'Origin': state.settings.public_url})


def run_for(state, private=True):
    store = state.store
    run = store.create_run(SECRET, '', 'modal', [], chat_enabled=True, user_id='google:alice',
                           private_owner_id='google:alice' if private else '')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest('capability'))
    return store.run(run['id'])


@pytest.mark.parametrize('user', ['alice', 'bob', 'admin'])
@pytest.mark.parametrize('private', [True, False])
def test_http_files_captures_attachments_owner_boundary(sinks, user, private):
    s, client = sinks
    login(s, client, 'alice')
    uploaded = client.put('/api/attachments/' + 'a'*32, params={'name': 'private.txt'}, content=SECRET.encode())
    assert uploaded.status_code == 200, uploaded.text
    run = run_for(s, private)
    with s.store.connect() as conn:
        s.store.attachments.bind_in(conn, ['a'*32], run['active_message_id'], 'google:alice')
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as handle:
        handle.writestr('new-files/private.txt', SECRET)
    s.store.artifacts.save(run['id']+'.zip', archive.getvalue())
    s.store.artifacts.save(run['id']+'-captures/private.png', PNG)
    login(s, client, user)
    expected = 200 if not private or user == 'alice' else 404
    base = '/api/runs/' + run['id']
    for url in [base+'/files', base+'/files/content?path=new-files/private.txt',
                base+'/files/content?path=new-files/private.txt&preview=true',
                base+'/computer/captures/private.png', '/api/attachments/'+'a'*32]:
        response = client.get(url)
        assert response.status_code == expected, (url, response.text)
        if expected == 404:
            assert SECRET not in response.text
    ranged = client.get(base+'/computer/captures/private.png', headers={'Range': 'bytes=8-13'})
    assert ranged.status_code == (206 if expected == 200 else 404)
    if expected == 200:
        assert client.get(base+'/files/content?path=new-files/private.txt').text == SECRET


@pytest.mark.parametrize('user', ['alice', 'bob', 'admin'])
def test_browser_internal_http_guard_precedes_cache_and_sandbox(sinks, user):
    s, client = sinks
    run = run_for(s)
    login(s, client, user)
    s.computer.sandbox = AsyncMock(return_value=None)
    url = '/api/runs/'+run['id']+'/computer'
    response = client.get(url)
    assert response.status_code == (200 if user == 'alice' else 404)
    if user != 'alice':
        s.computer.sandbox.assert_not_called()
        assert client.post(url, json={'action': 'screenshot'}).status_code == 404
        s.computer.sandbox.assert_not_called()


def test_private_exports_reject_stale_caller_metadata(sinks):
    s, _ = sinks
    run = run_for(s)
    forged = {**run, 'private_owner_id': ''}
    with s.store.connect() as conn, pytest.raises(HTTPException) as exc:
        s.media_shares.active(conn, forged)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        s.media_shares.source_bytes(forged, SimpleNamespace())
    with pytest.raises(HTTPException):
        s.memory.active(forged)
    with pytest.raises(HTTPException):
        captures.read(captures.directory(s.settings, run['id'])/'private.png', store=s.store)
    with pytest.raises(HTTPException):
        with artifact_files.saved_archive(s.settings, s.store, run['id']):
            pass
    assert not s.memory.tools(forged)
    assert not s.memory.context(forged)
    from app.skill_tools import save_skill
    with pytest.raises(HTTPException):
        save_skill(s.skills, forged, SimpleNamespace())


async def test_private_agent_and_automation_admission(sinks):
    s, _ = sinks
    run = run_for(s)
    from app.agents import AgentCoordinator
    coordinator = AgentCoordinator(s.store, s.settings, s.manager)
    assert not coordinator.available(run)
    with pytest.raises(HTTPException):
        await coordinator.fanout({**run, 'private_owner_id': ''}, Fanout(request_key='private-test', instructions=SECRET, items=['one'], workers=1))
    assert not s.store.rows('SELECT * FROM agent_groups')
    with s.store.connect() as conn:
        assert not AutomationEvents.human_session(conn, run['id'])
        with pytest.raises(HTTPException):
            s.automations.events.session_payload(conn, {'run_id':run['id']})
    assert not s.store.rows('SELECT * FROM automation_events')


def test_private_memory_review_and_slack_queue(sinks):
    s, _ = sinks
    run = run_for(s)
    s.store.finish_message(run['id'], run['active_message_id'], SECRET)
    assert not s.store.rows('SELECT * FROM memory_reviews')
    with s.store.connect() as conn:
        with pytest.raises(HTTPException):
            s.memory.save_in(conn, 'google:alice', SimpleNamespace(), source={'run_id': run['id']})
        s.slack.chat.queue(conn, run['id'], 'private-mirror', 'answer', SECRET)
        s.slack.chat.collect_answers_in(conn, {'run_id':run['id']}, True)
        s.slack.chat.collect_progress_in(conn, {'run_id':run['id']}, True)
        from app.pr_delivery import select_captures, select_prs
        assert select_prs(conn, run['id'], SECRET) == []
        assert select_captures(s.settings, run['id'], '/workspace/moyai-captures/private.png', store=s.store, conn=conn) == []
    assert not s.store.rows('SELECT * FROM slack_outbox')
    assert s.slack.chat.mirroring(run['id']) is None
    with pytest.raises(HTTPException):
        s.slack.channel.source_for_run(run['id'])


async def test_private_titles_do_not_invoke_model(sinks, monkeypatch):
    s, _ = sinks
    run = run_for(s)
    monkeypatch.setattr(s.session_titles, 'make_agent', lambda: pytest.fail('Private title sent to model'))
    await s.session_titles._generate(run['id'])
    assert not s.store.run(run['id'])['title_attempted_at']


async def test_private_trace_processor_and_direct_outbox(sinks):
    s, _ = sinks
    run = run_for(s)
    exported = []
    tracer = AgentTracing(s.store, s.settings, processor=SimpleNamespace(on_end=exported.append))
    tracer.enabled = True
    stale = {**run, 'private_owner_id': ''}
    s.store.tracing = tracer
    assert not s.manager.spec(stale)['tracing_enabled']
    tracer.model(stale, 'request', 1, [{'role':'user','content':SECRET}], {}, 'completed')
    tracer.tool(run['id'], {'name':'tool','input':SECRET,'output':SECRET})
    tracer.finish_turn(run['id'], run['active_message_id'], SECRET, 'completed')
    tracer.emit(stale, run['active_message_id'], 'private', 'span', 1, 2, {'input.value':SECRET})
    assert not exported
    assert not s.store.rows('SELECT * FROM trace_contexts')
    span = SimpleNamespace(attributes={'moyai.run_id':run['id']})
    for cls, args in [(TraceOutbox, ('trace_outbox','https://invalid.example/traces',{})),
                      (RaindropEventOutbox, ('https://invalid.example/events',{}))]:
        outbox = cls(s.store, *args)
        outbox.enqueue_payload(span, SECRET.encode())
        assert not s.store.rows('SELECT * FROM '+outbox.table)
        await outbox.close()


def test_private_public_media_denies_restored_snapshot_and_head(sinks, monkeypatch):
    import hashlib
    s, client = sinks
    run = run_for(s)
    token = 'a' * 43
    s.store.execute('''INSERT INTO media_shares
        (id,run_id,request_key,source,revision,token_hash,token_ciphertext,origin,reference,size,sha256,mime,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', ('b'*32, run['id'], 'legacy', 'capture:private.png', 'rev',
        hashlib.sha256(token.encode()).hexdigest(), '', s.settings.public_url, 'never-read', len(PNG),
        hashlib.sha256(PNG).hexdigest(), 'image/png', '2026-01-01'))
    monkeypatch.setattr(s.store.objects, 'read', lambda *args: pytest.fail('Private media bytes read'))
    for method in ('get', 'head'):
        response = getattr(client, method)('/media/'+'b'*32+'?token='+token)
        assert response.status_code == 404
        assert SECRET not in response.text
    with pytest.raises(HTTPException):
        s.media_shares.share(run, SimpleNamespace())
    assert len(s.store.rows('SELECT * FROM media_shares')) == 1


def test_attachment_rechecks_visibility_after_async_read(sinks, monkeypatch):
    s, client = sinks
    login(s, client, 'alice')
    assert client.put('/api/attachments/'+'c'*32, params={'name':'private.txt'}, content=SECRET.encode()).status_code == 200
    run = run_for(s)
    with s.store.connect() as conn:
        s.store.attachments.bind_in(conn, ['c'*32], run['active_message_id'], 'google:alice')
    original = s.store.attachments.payload
    def read_then_delete(*args):
        raw = original(*args)
        s.store.execute("UPDATE runs SET deleted_at='2026-01-01' WHERE id=?", (run['id'],))
        return raw
    monkeypatch.setattr(s.store.attachments, 'payload', read_then_delete)
    response = client.get('/api/attachments/'+'c'*32)
    assert response.status_code == 404
    assert SECRET not in response.text


async def test_private_slack_history_pending_delivery_and_status_are_suppressed(sinks):
    from app.agentchat_slack import SessionState
    s, _ = sinks
    run = run_for(s)
    s.store.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)',
                    ('T12345678','C12345678','123.456',run['id'],'123.456'))
    s.store.execute('INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,created_at,metadata) VALUES(?,?,?,?,?,?)',
                    (run['id'],'old-private','answer',SECRET,'2026-01-01','{}'))
    assert await SessionState(s.store).history('slack:T12345678:C12345678:123.456') == ()
    s.slack.connectors.request = AsyncMock(side_effect=AssertionError('Private Slack send'))
    await s.slack.chat.deliver_one()
    row = s.store.rows('SELECT * FROM slack_outbox')[0]
    assert row['status'] == 'skipped' and row['text'] == ''
    await s.slack.chat.activity.send(run['id'], SECRET)
    assert not s.store.rows('SELECT * FROM slack_activity')
    assert s.slack.chat.activity.status_for({'run_id':run['id']}) == ''
    with s.store.connect() as conn:
        s.slack.access.queue_in(conn, {'run_id':run['id']}, {}, 'pending')
    assert len(s.store.rows('SELECT * FROM slack_outbox')) == 1
    s.slack.connectors.request.assert_not_called()


async def test_private_restored_outbox_payload_cannot_export(sinks):
    import json
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    s, _ = sinks
    run = run_for(s)
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.attributes.add(key='moyai.run_id').value.string_value = run['id']
    span.attributes.add(key='input.value').value.string_value = SECRET
    for cls, args, payload in [
        (TraceOutbox, ('trace_outbox','https://invalid.example/traces',{}), request.SerializeToString()),
        (RaindropEventOutbox, ('https://invalid.example/events',{}),
         json.dumps({'properties':{'run_id':run['id']},'ai_data':{'input':SECRET}}).encode()),
    ]:
        outbox = cls(s.store, *args)
        s.store.execute(f'INSERT INTO {outbox.table}(trace_id,span_id,payload,created_at) VALUES(?,?,?,?)',
                        ('1','1',payload,0))
        outbox.client.post = AsyncMock(side_effect=AssertionError('Private trace export'))
        assert await outbox.export_once()
        outbox.client.post.assert_not_called()
        row = s.store.rows(f'SELECT * FROM {outbox.table}')[0]
        assert row['payload'] is None and row['delivered_at']
        await outbox.close()


def test_private_automation_direct_intake_denies_before_persistence(sinks):
    s, _ = sinks
    run = run_for(s)
    with s.store.connect() as conn, pytest.raises(HTTPException):
        s.automations.events.accept_in(conn, {'id':'unused'}, 'private-event',
            {'provider':'session','session_id':run['id'],'body':SECRET})
    assert not s.store.rows('SELECT * FROM automation_events')


def test_private_credentials_and_queue_admin_override_are_denied(sinks):
    from app.message_queue import MessageQueue
    s, _ = sinks
    run = run_for(s)
    with pytest.raises(HTTPException):
        s.credentials.request(run, SimpleNamespace())
    with pytest.raises(HTTPException):
        s.credentials.pending(run, 'google:admin', True)
    assert not s.store.rows('SELECT * FROM credential_requests')
    with pytest.raises(HTTPException) as exc:
        MessageQueue(s.store).change(run['id'], run['active_message_id'], 'google:admin', True, 1, 'edit', SECRET)
    assert exc.value.status_code == 404


def test_private_spend_retains_totals_without_titles_or_drilldowns(sinks):
    import json
    s, _ = sinks
    run = run_for(s)
    s.spend.begin(run, s.settings.agent_model)
    report = s.spend.report(user_id='google:alice')
    assert report['total']['requests'] == 1
    assert report['sessions'] == report['request_details'] == []
    assert SECRET not in json.dumps(report)
    assert run['id'] not in json.dumps(report)


def test_native_checkpoint_scope_binds_private_owner(sinks):
    from app.native_sessions import NativeSessions
    s, _ = sinks
    run = run_for(s)
    native = NativeSessions(s.settings, s.store, s.security, None, None, None)
    assert native.scope(run)['private_owner_id'] == 'google:alice'
    with pytest.raises(HTTPException):
        native.scope({**run, 'active_user_id':'google:bob'})
    org = run_for(s, private=False)
    assert 'private_owner_id' not in native.scope(org)


@pytest.mark.parametrize('user', ['alice', 'bob', 'admin'])
@pytest.mark.parametrize('kind', ['preview', 'audio'])
def test_private_attachment_sidechannels(sinks, user, kind):
    from PIL import Image
    s, client = sinks
    login(s, client, 'alice')
    run = run_for(s)
    attachment_id = 'd'*32
    if kind == 'preview':
        raw = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(raw, format='PNG')
        response = client.put('/api/attachments/'+attachment_id, params={'name':'private.png'}, content=raw.getvalue())
        assert response.status_code == 200
    else:
        # Seed already-transcribed audio; upstream transcription is unrelated to read ACLs.
        s.store.attachments.save(attachment_id, 'google:alice', 'private.wav', SECRET.encode(),
                                 ('audio/wav', b'', SECRET), 20 * 1024 * 1024)
    with s.store.connect() as conn:
        s.store.attachments.bind_in(conn, [attachment_id], run['active_message_id'], 'google:alice')
    login(s, client, user)
    headers = {'Range':'bytes=0-4'} if kind == 'audio' else {}
    response = client.get('/api/attachments/'+attachment_id+'/'+kind, headers=headers)
    assert response.status_code == ((206 if kind == 'audio' else 200) if user == 'alice' else 404)
    if user != 'alice':
        assert SECRET not in response.text


async def test_private_restored_review_job_is_skipped(sinks):
    s, _ = sinks
    run = run_for(s)
    s.store.finish_message(run['id'], run['active_message_id'], SECRET)
    s.store.execute('''INSERT INTO memory_reviews
        (message_id,run_id,actor_id,owner_id,preferences_revision,available_at,updated_at)
        VALUES(?,?,?,?,1,'2020-01-01','2020-01-01')''',
        (run['active_message_id'], run['id'], 'google:alice', 'google:alice'))
    s.settings.litellm_api_key = 'synthetic-never-sent'
    s.settings.litellm_api_base = 'https://invalid.example/v1'
    s.memory_review.extract = AsyncMock(side_effect=AssertionError('Private review sent to model'))
    assert await s.memory_review.process_next()
    assert s.store.rows('SELECT status FROM memory_reviews')[0]['status'] == 'skipped'
    s.memory_review.extract.assert_not_called()
    assert not s.store.rows('SELECT * FROM personal_memories')
