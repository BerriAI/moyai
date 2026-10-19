"""Real product identities/native PostgreSQL with a deterministic Slack membership API."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_identity_admission as identities
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import text, update

from moyai.contracts import NewMessage, NewSession
from moyai.inbound_events import InboundEvent
from moyai.models import Session, SlackThread, Turn, User
from moyai.product_routes import ProductReadService, Reader
from moyai.product_slack_models import ProductSlackBinding
from moyai.slack_audience import can_read
from moyai.slack_audience_reads import AUDIENCE_TTL, SlackAudienceReads

pytestmark = pytest.mark.skipif(not os.getenv('MOYAI_TEST_POSTGRES'), reason='Requires local PostgreSQL')
product_database = identities.product_database
admitted_product = identities.admitted_product
slack_admission = identities.slack_admission


@pytest_asyncio.fixture
async def audience(slack_admission):
    product = slack_admission
    state = {'members': ['UACTUAL01'], 'private': True, 'authority': 'a' * 64,
             'failure': None, 'calls': [], 'clock': 0}
    async def token():
        return 'synthetic-membership-token'
    async def request(method, url, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer synthetic-membership-token'
        state['calls'].append(url)
        if state['failure']:
            raise state['failure']
        if url.endswith('conversations.info'):
            return {'ok': True, 'channel': {'id': kwargs['params']['channel'], 'is_channel': True,
                                            'is_private': state['private'], 'is_ext_shared': False}}
        assert url.endswith('conversations.members')
        return {'ok': True, 'members': state['members'], 'response_metadata': {'next_cursor': ''}}
    slack = SimpleNamespace(runtime=SimpleNamespace(database=product.database),
        installation=lambda: {'team_id': 'TLOCAL001', 'authority': state['authority']},
        connectors=SimpleNamespace(slack_bot_token=token, request=request),
        audience_reads=SlackAudienceReads(clock=lambda: state['clock']))
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


@pytest.mark.parametrize('change', ['membership', 'installation'])
async def test_revocation_blocks_transcript_inventory_search_and_history(audience, change):
    product = audience
    assert (await product.database.messages(product.actor, product.session_id))[0]['content'] == 'Private channel input'
    if change == 'membership':
        product.state['members'] = []
        product.state['clock'] += AUDIENCE_TTL
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


@pytest.mark.parametrize('failure, status, code', [
    (TimeoutError('private-provider-body'), 503, 'timeout'),
    (ConnectorError('private-provider-body', code='rate_limited', status_code=429,
                    retryable=True, retry_after=3.5), 503, 'rate_limited'),
    (ConnectorError('private-provider-body', code='provider_http_error', status_code=503, retryable=True),
     503, 'provider_http_error'),
    (ConnectorError('private-provider-body', code='missing_scope'), 404, 'missing_scope'),
    (ConnectorError('private-provider-body', code='not_in_channel'), 404, 'not_in_channel'),
    (ValueError('private-provider-body'), 503, 'provider_unavailable'),
])
async def test_provider_failure_is_safe_distinct_and_does_not_cache_a_denial(audience, caplog, failure, status, code):
    product = audience
    product.state['failure'] = failure
    # Include a request-local cache: unavailable reads must be retried, not saved
    # as a denial for the rest of this request/stream.
    product.actor.slack_read_cache = {}
    with pytest.raises(HTTPException) as error:
        await product.database.messages(product.actor, product.session_id)
    assert error.value.status_code == status
    assert 'reconnect' not in error.value.detail.lower()
    assert 'private-provider-body' not in error.value.detail + caplog.text
    assert 'synthetic-membership-token' not in caplog.text
    assert f'code={code}' in caplog.text
    if code == 'rate_limited':
        assert error.value.headers == {'Retry-After': '4'}
    if status == 503:
        for operation in (
            lambda: product.database.list_sessions(product.actor),
            lambda: product.reads.listing(product.reader, source='native'),
            lambda: product.reads.native_search(product.reader, 'Private', limit=20, offset=0),
        ):
            with pytest.raises(HTTPException) as unavailable:
                await operation()
            assert unavailable.value.status_code == 503
    product.state['failure'] = None
    product.state['clock'] += AUDIENCE_TTL
    assert (await product.database.messages(product.actor, product.session_id))[0]['content'] == 'Private channel input'


async def test_dm_only_allows_current_verified_requester(audience):
    product = audience
    async with product.database.sessions.begin() as db:
        binding = await db.get(ProductSlackBinding, product.session_id)
        binding.channel_id = 'DPRIVATE1'
    product.state['failure'] = ConnectorError('No new DM scopes', code='missing_scope')
    assert (await product.database.messages(product.actor, product.session_id))[0]['is_self']
    assert product.state['calls'] == []
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
            product.state['clock'] += AUDIENCE_TTL
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


async def test_worker_keeps_saved_authority_while_new_reads_and_inputs_require_membership(audience):
    database = audience.database
    turn = await database.claim_next()
    prepared = await database.prepare(turn.id)
    await database.authorize_scope(prepared.scope)
    audience.state['members'] = []
    await database.authorize_scope(prepared.scope)
    with pytest.raises(HTTPException) as denied:
        await database.submit(audience.actor, audience.session_id, NewMessage(prompt='New input', request_key='new-input'))
    assert denied.value.status_code == 404
    with pytest.raises(HTTPException):
        await database.messages(audience.actor, audience.session_id)


async def test_stream_and_browser_reads_share_provider_snapshot_but_keep_local_revocation(audience):
    from moyai.live_stream import authorized_events

    product = audience
    async def authorize():
        # The actual stream discards its request-local cache at each event.
        product.actor.slack_read_cache = {}
        await product.database.session(product.actor, product.session_id)

    async def disconnected():
        return False

    async def events():
        for i in range(80):
            yield f'data: {i}\n\n'

    items = [item async for item in authorized_events(events(), authorize, disconnected)]
    assert len(items) == 80
    assert product.state['calls'] == [
        'https://slack.com/api/conversations.info', 'https://slack.com/api/conversations.members',
    ]
    for _ in range(5):
        await authorize()
        await product.database.list_sessions(product.actor)
        await product.reads.messages(product.reader, product.session_id, 'native')
    assert len(product.state['calls']) == 2

    # A local installation revocation is immediate even with a fresh snapshot.
    product.state['authority'] = 'b' * 64
    with pytest.raises(HTTPException) as denied:
        await authorize()
    assert denied.value.status_code == 404
    assert len(product.state['calls']) == 2


async def test_membership_snapshot_expires_and_never_survives_provider_failure(audience):
    product = audience
    assert await product.database.messages(product.actor, product.session_id)
    product.state['members'] = []
    product.state['clock'] = AUDIENCE_TTL - 1
    assert await product.database.messages(product.actor, product.session_id)
    product.state['clock'] = AUDIENCE_TTL
    product.state['failure'] = ConnectorError('limited', code='rate_limited', status_code=429,
                                             retryable=True, retry_after=10)
    with pytest.raises(HTTPException) as unavailable:
        await product.database.messages(product.actor, product.session_id)
    assert unavailable.value.status_code == 503
    product.state['failure'] = None
    product.state['clock'] += 10
    with pytest.raises(HTTPException) as denied:
        await product.database.messages(product.actor, product.session_id)
    assert denied.value.status_code == 404


@pytest_asyncio.fixture
async def connected_audience(audience, monkeypatch):
    """Real saved Slack identity and GitHub grants, replacing only provider I/O."""
    from moyai.product_connections import ProductConnections

    product = audience
    async with product.source.begin() as db:
        for statement in (
            'ALTER TABLE moyai.connection_policies ADD COLUMN checked_at TEXT, ADD COLUMN check_status TEXT',
            'CREATE TABLE moyai.connection_audit (id BIGINT PRIMARY KEY, provider TEXT)',
            'CREATE TABLE moyai.personal_slack_grants (owner_id TEXT PRIMARY KEY, encrypted TEXT)',
            'CREATE TABLE moyai.github_app (id INTEGER PRIMARY KEY, encrypted TEXT)',
        ):
            await db.execute(text(statement))
        encrypted = product.adapter.legacy_security.encrypt(json.dumps({
            'kind': 'github_app', 'installation_id': 1, 'account_id': 1,
            'repository_ids': list(range(1, 9)),
        }))
        await db.execute(text("INSERT INTO moyai.connections VALUES('github',:encrypted)"), {'encrypted': encrypted})
        await db.execute(text("INSERT INTO moyai.connection_policies(provider,enabled,read_only) VALUES('github',1,1)"))
    await product.source.dispose()
    await product.database.engine.dispose()

    class TransactionOnlyStore:
        schema_updates = False

        def rows(self, *args, **kwargs):
            pytest.fail('Execution authorization must read grants in its native transaction.')

    service = ProductConnections(SimpleNamespace(
        store_proxy=TransactionOnlyStore(), identity=product.adapter,
        security=product.adapter.legacy_security, settings=product.adapter.settings,
    ), product.database)
    product.database.product_connections = service
    product.connections = service
    monkeypatch.setattr(service.connectors.github, 'repository_name', lambda identity: f'example/repo-{identity}')
    # Finish the fixture's first input, then admit a real signed follow-up with
    # multiple selected repositories through the normal transactional path.
    first = await product.database.claim_next()
    await product.database.fail(first.id, 'Fixture setup complete')
    async with product.database.sessions.begin() as db:
        sender = await product.adapter.project_native_user(db, product.raw_principal)
    sender.slack_channel_evidence = ('TLOCAL001', 'GPRIVATE1')
    product.turn = await product.database.submit(sender, product.session_id, NewMessage.from_event(InboundEvent(
        source='slack', sender_id=sender.id, deduplication_key='connected-slack', text='Hello from Slack',
        provenance={'team_id': 'TLOCAL001', 'channel_id': 'GPRIVATE1',
                    'thread_ts': '1792255700.001', 'message_ts': '1792255700.002', 'user_id': 'UACTUAL01'},
    ), connections=['github']))
    assert (await product.database.claim_next()).id == product.turn.id
    return product


@pytest.mark.parametrize('failure', [
    TimeoutError(),
    ConnectorError('Rate limited', code='rate_limited', status_code=429, retryable=True, retry_after=30),
    ConnectorError('Existing scopes only', code='missing_scope'),
])
async def test_slack_turn_reaches_model_with_parallel_repository_discovery_during_outage(
    connected_audience, monkeypatch, failure,
):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai_harness.github import GitHub

    from moyai.connections import connection_tools

    product = connected_audience
    prepared = await product.database.prepare(product.turn.id)
    product.state['failure'] = failure
    assert len(prepared.scope.grants) == 8
    barrier, credentials = asyncio.Barrier(8), []

    async def provider_credentials(grant, *, write):
        assert not write
        credentials.append(grant.id)
        await asyncio.wait_for(barrier.wait(), 5)
        return {'access_token': 'synthetic-github-token'}

    def provider_catalog(self):
        tools = FunctionToolset(id=self.id)
        tools.add_function(lambda owner, repo: 'unused', name='read_repository',
                           metadata={'annotations': {'readOnlyHint': True}})
        return tools

    monkeypatch.setattr(product.connections, '_credentials', provider_credentials)
    monkeypatch.setattr(GitHub, 'get_toolset', provider_catalog)
    monkeypatch.setattr('moyai.runtime.current', lambda: SimpleNamespace(product_connections=product.connections))
    model = TestModel(call_tools=[], custom_output_text='Hello back')
    agent = Agent(model, capabilities=[connection_tools(product.database.security)])
    result = await agent.run(prepared.prompt, deps=prepared.scope)
    assert result.output == 'Hello back' and result.usage.requests == 1
    assert len(credentials) == 8
    assert len([tool for tool in model.last_model_request_parameters.function_tools
                if tool.name.endswith('_read_repository')]) == 8
    assert product.state['calls'] == []  # Admission, prepare and 16 credential fences: no Slack API.


@pytest.mark.parametrize('revoke', ['installation', 'sender', 'connection', 'policy', 'stopped', 'deleted'])
async def test_slack_execution_still_rejects_saved_authority_changes(connected_audience, revoke):
    product = connected_audience
    prepared = await product.database.prepare(product.turn.id)
    await product.database.authorize_scope(prepared.scope)
    if revoke in {'installation', 'connection', 'policy'}:
        async with product.source.begin() as db:
            if revoke == 'installation':
                await db.execute(text("DELETE FROM moyai.connections WHERE provider='slack'"))
            elif revoke == 'connection':
                await db.execute(text("DELETE FROM moyai.connections WHERE provider='github'"))
            else:
                await db.execute(text("UPDATE moyai.connection_policies SET enabled=0 WHERE provider='github'"))
    else:
        async with product.database.sessions.begin() as db:
            if revoke == 'sender':
                await db.execute(update(User).where(User.id == product.raw_principal.id).values(enabled=False))
            elif revoke == 'deleted':
                await db.execute(update(Session).where(Session.id == product.session_id).values(deleted=True))
            else:
                await db.execute(update(Turn).where(Turn.id == product.turn.id).values(status='stopped'))
    with pytest.raises((HTTPException, PermissionError)):
        await product.database.authorize_scope(prepared.scope)
    assert product.state['calls'] == []


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
    from moyai.product_history import ProductHistoryAdapter
    product.slack.runtime.product_history = ProductHistoryAdapter(product.adapter, product.source, schema='moyai')
    async with product.source.begin() as db:
        for ddl in (
            "CREATE TABLE moyai.runs(id text PRIMARY KEY,owner_id text,parent_run_id text DEFAULT '')",
            'CREATE TABLE moyai.run_roots(run_id text,root_id text)',
            'CREATE TABLE moyai.slack_threads(run_id text,team_id text,channel text,thread_ts text)',
            'CREATE TABLE moyai.slack_events(run_id text,user_id text,created_at text)',
            'CREATE TABLE moyai.messages(id integer,run_id text,user_id text)',
            'CREATE TABLE moyai.slack_receipts(message_id integer,run_id text,team_id text,channel text,message_ts text,user_id text)',
        ):
            await db.execute(text(ddl))
        await db.execute(text('INSERT INTO moyai.runs(id,owner_id) VALUES(:id,:actor)'),
                         {'id': source_id, 'actor': product.raw_principal.id})
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
    product.state['clock'] += AUDIENCE_TTL
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


async def test_rejected_refresh_token_explains_connection_recovery_without_exposing_provider_data(audience):
    product = audience
    product.state['failure'] = ConnectorError('private-provider-body', code='invalid_refresh_token')
    with pytest.raises(HTTPException) as error:
        await product.database.messages(product.actor, product.session_id)
    assert error.value.status_code == 503
    assert 'reconnect Slack' in error.value.detail and 'Settings → Connections' in error.value.detail
    assert 'private-provider-body' not in error.value.detail


async def test_failure_report_exports_slack_trace_with_saved_authority(audience, monkeypatch):
    from sqlalchemy import select

    from moyai.failure_report import FailureReport
    async with audience.database.sessions.begin() as db:
        turn = await db.scalar(select(Turn).where(Turn.session_id == audience.session_id))
        turn.status, turn.error = 'failed', 'The agent could not complete this turn.'

    async def export(runtime, actor, session_id):
        session = await runtime.database.session(actor, session_id)
        return {'session_id': session.id, 'steps': []}

    monkeypatch.setattr('moyai.failure_report.debug_trace', export)
    report = await FailureReport.for_turn(SimpleNamespace(database=audience.database), turn)
    assert json.loads(report.debug_log.content) == {'session_id': audience.session_id, 'steps': []}
