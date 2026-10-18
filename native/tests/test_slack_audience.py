"""Real product identities/native PostgreSQL with a deterministic Slack membership API."""

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_identity_admission as identities
from fastapi import HTTPException
from sqlalchemy import text

from moyai.contracts import NewMessage, NewSession
from moyai.inbound_events import InboundEvent
from moyai.models import Session, SlackThread
from moyai.product_routes import ProductReadService, Reader
from moyai.product_slack_models import ProductSlackBinding
from moyai.slack_audience import can_read

pytestmark = pytest.mark.skipif(not os.getenv('MOYAI_TEST_POSTGRES'), reason='Requires local PostgreSQL')
product_database = identities.product_database
admitted_product = identities.admitted_product
slack_admission = identities.slack_admission


@pytest_asyncio.fixture
async def audience(slack_admission):
    product = slack_admission
    state = {'members': ['UACTUAL01'], 'private': True, 'authority': 'a' * 64, 'failed': False}
    async def token():
        return 'synthetic-membership-token'
    async def request(method, url, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer synthetic-membership-token'
        if state['failed']:
            raise TimeoutError()
        if url.endswith('conversations.info'):
            return {'ok': True, 'channel': {'id': kwargs['params']['channel'], 'is_channel': True,
                                            'is_private': state['private'], 'is_ext_shared': False}}
        assert url.endswith('conversations.members')
        return {'ok': True, 'members': state['members'], 'response_metadata': {'next_cursor': ''}}
    slack = SimpleNamespace(runtime=SimpleNamespace(database=product.database),
        installation=lambda: {'team_id': 'TLOCAL001', 'authority': state['authority']},
        connectors=SimpleNamespace(slack_bot_token=token, request=request))
    product.database.slack_access = lambda db, session, actor: can_read(slack, db, session, actor)
    async with product.database.sessions.begin() as db:
        sender = await product.adapter.project_native_user(db, product.raw_principal)
    sender.slack_channel_evidence = ('TLOCAL001', 'GPRIVATE1')
    session = await product.database.create_session(sender, NewSession(title='Private Slack task'))
    async with product.database.sessions.begin() as db:
        (await db.get(Session, session.id)).surface = 'slack'
        db.add(SlackThread(session_id=session.id, team_id='TLOCAL001', channel_id='GPRIVATE1', thread_ts='1792255700.001'))
        db.add(ProductSlackBinding(session_id=session.id, requester_id=sender.id, team_id='TLOCAL001',
            channel_id='GPRIVATE1', thread_ts='1792255700.001', started_ts='1792255700.001', connection_authority='a' * 64))
    body = NewMessage.from_event(InboundEvent(
        source='slack', sender_id=sender.id, deduplication_key='private-slack', text='Private channel input',
        provenance={'team_id': 'TLOCAL001', 'channel_id': 'GPRIVATE1',
                    'thread_ts': '1792255700.001', 'message_ts': '1792255700.001', 'user_id': 'UACTUAL01'},
    ))
    await product.database.submit(sender, session.id, body)
    del sender.slack_channel_evidence
    runtime = SimpleNamespace(database=product.database, security=product.database.security,
                              product_identity=product.adapter)
    product.reads = ProductReadService(runtime)
    product.reader = Reader(product.actor, product.principal)
    product.session_id, product.state, product.slack = session.id, state, slack
    return product


async def test_linked_self_and_capabilities_use_current_verified_profile(audience):
    product = audience
    rows = await product.database.messages(product.actor, product.session_id)
    assert rows[0]['author_name'] == 'Signed Slack sender'
    assert rows[0]['is_self'] and rows[0]['source'] == 'slack'
    detail = await product.reads.detail(product.reader, product.session_id, 'native')
    assert detail['session']['can_manage']
    async with product.source.begin() as db:
        await db.execute(text('UPDATE moyai.users SET profile_conflict=1 WHERE id=:id'), {'id': product.raw_principal.id})
    with pytest.raises(HTTPException) as error:
        await product.database.messages(product.actor, product.session_id)
    assert error.value.status_code == 404


@pytest.mark.parametrize('change', ['membership', 'provider', 'installation'])
async def test_revocation_blocks_transcript_inventory_search_and_history(audience, change):
    product = audience
    assert (await product.database.messages(product.actor, product.session_id))[0]['content'] == 'Private channel input'
    if change == 'membership':
        product.state['members'] = []
    elif change == 'provider':
        product.state['failed'] = True
    else:
        product.state['authority'] = 'b' * 64
    for operation in (
        product.database.session(product.actor, product.session_id),
        product.database.messages(product.actor, product.session_id),
        product.reads.messages(product.reader, product.session_id, 'native'),
    ):
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 404
    assert await product.database.list_sessions(product.actor) == []
    assert (await product.reads.listing(product.reader, source='native'))['sessions'] == []
    assert (await product.reads.native_search(product.reader, 'Private', limit=20, offset=0))['sessions'] == []


async def test_dm_only_allows_current_verified_requester(audience):
    product = audience
    async with product.database.sessions.begin() as db:
        binding = await db.get(ProductSlackBinding, product.session_id)
        binding.channel_id = 'DPRIVATE1'
    assert (await product.database.messages(product.actor, product.session_id))[0]['is_self']
    async with product.database.sessions.begin() as db:
        binding = await db.get(ProductSlackBinding, product.session_id)
        binding.requester_id = 'slack:TLOCAL001:UOTHER001'
    with pytest.raises(HTTPException):
        await product.database.messages(product.actor, product.session_id)


async def test_revocation_blocks_file_export_and_live_endpoints(audience, runtime):
    import httpx

    from moyai.api import create_app
    from moyai.file_preview import FilePreviewService
    from moyai.models import Upload
    product = audience
    original = runtime.database
    runtime.database = product.database
    try:
        app = create_app(runtime, run_dispatcher=False)
        route = next(route for route in app.routes if getattr(route, 'path', '') == '/api/sessions/{session_id}')
        dependency = next(item.call for item in route.dependant.dependencies if item.name == 'user')
        app.dependency_overrides[dependency] = lambda: product.actor
        raw = b'private source attachment'
        digest = await runtime.objects.put(raw)
        async with product.database.sessions.begin() as db:
            upload = Upload(owner_id=product.actor.id, session_id=product.session_id, name='private.txt',
                            media_type='text/plain', size=len(raw), digest=digest)
            db.add(upload)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/uploads/' + upload.id)).status_code == 200
            previews = FilePreviewService(runtime)
            assert (await previews.record(product.actor, product.session_id, 'upload', upload.id)).name == 'private.txt'
            product.state['members'] = []
            with pytest.raises(HTTPException) as denied:
                await previews.record(product.actor, product.session_id, 'upload', upload.id)
            assert denied.value.status_code == 404
            for path in ('/api/uploads/' + upload.id,
                         f'/api/sessions/{product.session_id}/debug-trace',
                         f'/api/sessions/{product.session_id}/progress/unused',
                         f'/api/sessions/{product.session_id}/events?turn_id=unused'):
                response = await client.get(path)
                assert response.status_code == 404, (path, response.text)
                assert raw.decode() not in response.text
    finally:
        runtime.database = original


async def test_worker_reuses_admitted_grant_and_rechecks_channel_membership(audience):
    database = audience.database
    turn = await database.claim_next()
    prepared = await database.prepare(turn.id)
    await database.authorize_scope(prepared.scope)
    audience.state['members'] = []
    with pytest.raises(HTTPException) as denied:
        await database.authorize_scope(prepared.scope)
    assert denied.value.status_code == 404


async def test_idle_native_sender_refresh_restores_verified_self_without_new_message(audience):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from moyai.slack_profile_refresh import watch
    product = audience
    async with product.source.begin() as db:
        await db.execute(text('ALTER TABLE moyai.users ADD COLUMN profile_next_check INTEGER NOT NULL DEFAULT 0'))
        await db.execute(text("UPDATE moyai.users SET profile_checked_at=:stamp,profile_next_check=:future WHERE kind='slack'"), {
            'stamp': (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            # Older releases used a daily timer; last successful verification
            # must still trigger refresh before the one-hour trust window ends.
            'future': int((datetime.now(UTC) + timedelta(hours=8)).timestamp()),
        })
        await db.execute(text("INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at) "
                              "VALUES('slack:TLOCAL001:UUNUSED01','slack','','Unrelated participant','c','u')"))
    await product.source.dispose()
    await product.database.engine.dispose()
    with pytest.raises(HTTPException):
        await product.database.messages(product.actor, product.session_id)
    refreshed = asyncio.Event()
    calls = []
    async def refresh(team, user, *, refresh_profile=False):
        calls.append((team, user, refresh_profile))
        # Deterministic successful users.info boundary; the canonical resolver's
        # invalid/changed/transient-profile behavior is tested independently.
        async with product.source.begin() as db:
            await db.execute(text("UPDATE moyai.users SET profile_checked_at=:stamp,name='Current sender' WHERE id=:id"),
                             {'stamp': datetime.now(UTC).isoformat(), 'id': product.raw_principal.id})
        current = await product.adapter.current(product.raw_principal.id)
        async with product.database.sessions.begin() as db:
            await product.adapter.project_native_user(db, current)
        refreshed.set()
    slack = SimpleNamespace(
        settings=SimpleNamespace(slack_identity_linking_enabled=True),
        installation=product.slack.installation, product=SimpleNamespace(identity=product.adapter),
        runtime=SimpleNamespace(database=product.database), actor=refresh,
    )
    task = asyncio.create_task(watch(slack))
    try:
        await asyncio.wait_for(refreshed.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert calls == [('TLOCAL001', 'UACTUAL01', True)]
    messages = await product.database.messages(product.actor, product.session_id)
    assert len(messages) == 1 and messages[0]['is_self'] and messages[0]['author_name'] == 'Current sender'


async def test_historical_reads_use_native_controls_and_preserve_verified_attribution(audience):
    from moyai.slack_audience import historical_access, historical_messages
    product = audience
    source_id = 'a' * 32
    product.slack.runtime.product_history = SimpleNamespace(table=lambda name: f'moyai.{name}')
    async with product.source.begin() as db:
        for ddl in (
            'CREATE TABLE moyai.runs(id text PRIMARY KEY,owner_id text)',
            'CREATE TABLE moyai.run_roots(run_id text,root_id text)',
            'CREATE TABLE moyai.slack_threads(run_id text,team_id text,channel text,thread_ts text)',
            'CREATE TABLE moyai.slack_events(run_id text,user_id text,created_at text)',
            'CREATE TABLE moyai.messages(id integer,run_id text,user_id text)',
            'CREATE TABLE moyai.slack_receipts(message_id integer,run_id text,team_id text,channel text,message_ts text,user_id text)',
        ):
            await db.execute(text(ddl))
        await db.execute(text('INSERT INTO moyai.runs VALUES(:id,:actor)'), {'id': source_id, 'actor': product.raw_principal.id})
        await db.execute(text('INSERT INTO moyai.run_roots VALUES(:id,:id)'), {'id': source_id})
        await db.execute(text("INSERT INTO moyai.slack_threads VALUES(:id,'TLOCAL001','GPRIVATE1','1792255700.001')"), {'id': source_id})
        await db.execute(text("INSERT INTO moyai.slack_events VALUES(:id,'UACTUAL01','2026-10-17')"), {'id': source_id})
        await db.execute(text('INSERT INTO moyai.messages VALUES(1,:id,:actor)'), {'id': source_id, 'actor': product.raw_principal.id})
        await db.execute(text("INSERT INTO moyai.slack_receipts VALUES(1,:id,'TLOCAL001','GPRIVATE1','1792255700.002','UACTUAL01')"), {'id': source_id})
    async with product.source.connect() as db:
        assert await historical_access(product.slack, db, product.principal, [source_id]) == {source_id}
        rows = [{'id': 1, 'role': 'user', 'author_id': product.raw_principal.id,
                 'content': 'Slack reply from UACTUAL01:\nOnly the authored text'}]
        await historical_messages(product.slack, db, product.principal, source_id, rows)
        assert rows[0]['content'] == 'Only the authored text'
        assert rows[0]['source'] == 'slack' and rows[0]['is_self']
    product.state['members'] = []
    async with product.source.connect() as db:
        assert await historical_access(product.slack, db, product.principal, [source_id]) == set()


async def test_linked_owner_can_find_delete_and_restore_their_slack_conversation(audience):
    product = audience
    listing = await product.reads.listing(product.reader, source='native')
    assert listing['sessions'][0]['can_manage']
    await product.database.delete_session(product.actor, product.session_id)
    assert [row.id for row in await product.database.list_sessions(product.actor, deleted=True)] == [product.session_id]
    assert (await product.reads.listing(product.reader, source='native', deleted=True))['sessions'][0]['can_manage']
    await product.database.restore_session(product.actor, product.session_id)
    assert (await product.database.messages(product.actor, product.session_id))[0]['is_self']
