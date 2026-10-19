"""Real native transcripts and product cache; deterministic Slack profile boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from test_application_boundaries import sign_in
from test_product_connections import bridge as canonical_connections

from moyai.contracts import NewMessage, NewSession
from moyai.inbound_events import InboundEvent
from moyai.models import Message, Session, Turn
from moyai.slack_mentions import SlackMentionNames

TEAM, PERSON = 'TTEST0001', 'U0B1TDTHL4Q'
TEXT = f'make an IMG of <@{PERSON}>’s face and send it here'
bridge = canonical_connections


@pytest.fixture
def profiles(bridge, monkeypatch):
    state = {'calls': [], 'name': 'Jordan Lee', 'failure': None,
             'installed': {'team_id': TEAM, 'authority': 'installation-one', 'scopes': ['users:read']}}

    async def token():
        return 'synthetic-bot-token'

    async def request(method, url, **kwargs):
        assert method == 'GET' and url == 'https://slack.com/api/users.info'
        assert kwargs['headers'] == {'Authorization': 'Bearer synthetic-bot-token'}
        state['calls'].append(kwargs['params']['user'])
        await asyncio.sleep(0)
        if state['failure']:
            raise state['failure']
        if state.get('on_read'):
            await state['on_read']()
        return state.get('response', {'ok': True, 'user': {
            'id': kwargs['params']['user'], 'team_id': TEAM,
            'profile': {'real_name': state['name'], 'display_name': 'Nickname'},
        }})

    monkeypatch.setattr(bridge.connectors, 'slack_bot_token', token)
    monkeypatch.setattr(bridge.connectors, 'request', request)
    resolver = SlackMentionNames(SimpleNamespace(store=bridge.store, connectors=bridge.connectors,
                                                installation=lambda: dict(state['installed'])))
    return resolver, state, bridge.store


async def admit(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    body = NewMessage.from_event(InboundEvent(
        source='slack', sender_id=actor.id, deduplication_key='mention-source', text=TEXT,
        provenance={'team_id': TEAM, 'channel_id': 'CTEST0001', 'user_id': 'UAUTHOR01',
                    'thread_ts': '1792255700.001', 'message_ts': '1792255700.001'},
    ))
    turn = await runtime.database.submit(actor, session.id, body)
    return session, turn


async def test_native_api_resolves_existing_mentions_without_changing_text_or_enrolling_users(runtime, actor, profiles):
    resolver, state, store = profiles
    session, turn = await admit(runtime, actor)  # Already-saved rows need no migration.
    runtime.database.slack_mentions = resolver.decorate
    before = store.rows('SELECT id FROM users ORDER BY id')
    async with sign_in(runtime) as client:
        detail = await client.get(f'/api/sessions/{session.id}?include_messages=true')
        assert detail.status_code == 200, detail.text
        message = detail.json()['messages'][0]
        assert message['content'] == TEXT
        assert message['slack_mentions'] == {PERSON: 'Jordan Lee'}
        page = await client.get(f'/api/sessions/{session.id}/messages')
        assert page.json()[0]['slack_mentions'] == message['slack_mentions']
        history = await client.get(f'/api/history/sessions/{session.id}/messages?source=native')
        assert history.status_code == 200, history.text
        assert history.json()['messages'][0]['slack_mentions'] == message['slack_mentions']
        state['name'] = 'Jordan Updated'
        store.execute('UPDATE slack_mention_names SET next_check=0')
        refreshed = (await client.get(f'/api/sessions/{session.id}?include_messages=true')).json()
        assert refreshed['messages'][0]['slack_mentions'] == {PERSON: 'Jordan Updated'}
        assert refreshed['transcript_version'] != detail.json()['transcript_version']
    assert state['calls'] == [PERSON, PERSON]
    assert store.rows('SELECT id FROM users ORDER BY id') == before
    async with runtime.database.sessions() as db:
        saved = await db.scalar(select(Message).where(Message.turn_id == turn.id))
        assert runtime.security.unseal(saved.content).decode() == TEXT
        assert runtime.security.unseal((await db.get(Turn, turn.id)).prompt).decode() == TEXT


async def test_cached_profiles_are_team_scoped_and_parallel_reads_share_lookup(profiles):
    resolver, state, store = profiles
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', ('TOTHER001', PERSON, 'Wrong workspace', 0))
    names = await asyncio.gather(*(resolver.names(TEAM, [PERSON]) for _ in range(3)))
    assert names == [{PERSON: 'Jordan Lee'}] * 3
    assert state['calls'] == [PERSON]
    # Reuse the persistent cache after a process restart.
    assert await SlackMentionNames(resolver.slack).names(TEAM, [PERSON]) == names[0]
    assert state['calls'] == [PERSON]


async def test_existing_sender_name_wins_without_a_provider_lookup(profiles):
    resolver, state, store = profiles
    with store.connect() as db:
        identity = store.slack_identity_in(db, TEAM, PERSON)
    store.execute('UPDATE users SET name=? WHERE id=?', ('Verified sender', identity))
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', (TEAM, PERSON, 'Old name', 0))
    assert await resolver.names(TEAM, [PERSON]) == {PERSON: 'Verified sender'}
    assert state['calls'] == []


@pytest.mark.parametrize('response', [
    {'ok': False, 'error': 'missing_scope'},
    {'ok': True, 'user': {'id': PERSON, 'team_id': 'TOTHER001', 'profile': {'real_name': 'Wrong team'}}},
    {'ok': True, 'user': {'id': 'UOTHER001', 'team_id': TEAM, 'profile': {'real_name': 'Wrong user'}}},
    {'ok': True, 'user': {'id': PERSON, 'team_id': TEAM, 'profile': 'invalid'}},
])
async def test_unverified_profiles_keep_original_id_and_back_off(profiles, response):
    resolver, state, store = profiles
    state['response'] = response
    assert await resolver.names(TEAM, [PERSON]) == {}
    assert await resolver.names(TEAM, [PERSON]) == {}
    assert state['calls'] == [PERSON]
    assert store.rows('SELECT name FROM slack_mention_names') == [{'name': ''}]


async def test_failure_retains_last_known_name_and_rotation_discards_lookup(profiles):
    resolver, state, store = profiles
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', (TEAM, PERSON, 'Last known name', 0))
    state['failure'] = TimeoutError('provider unavailable')
    assert await resolver.names(TEAM, [PERSON]) == {PERSON: 'Last known name'}
    state['failure'] = None
    store.execute('UPDATE slack_mention_names SET next_check=0')
    async def rotate():
        state['installed']['authority'] = 'replacement-installation'
    state['on_read'] = rotate
    assert await resolver.names(TEAM, [PERSON]) == {PERSON: 'Last known name'}
    assert store.rows('SELECT name FROM slack_mention_names') == [{'name': 'Last known name'}]


async def test_only_proven_slack_user_messages_resolve_and_no_scope_uses_cache(profiles):
    resolver, state, store = profiles
    rows = [{'id': 1, 'role': 'user', 'content': TEXT}, {'id': 2, 'role': 'assistant', 'content': TEXT},
            {'id': 3, 'role': 'user', 'content': TEXT}]
    await resolver.decorate(rows, {1: {'kind': 'web'}, 2: {'kind': 'slack', 'team_id': TEAM},
                                    3: {'kind': 'slack', 'team_id': TEAM, 'deleted': True}})
    assert all('slack_mentions' not in row for row in rows)
    assert state['calls'] == []
    state['installed']['scopes'] = []
    assert await resolver.names(TEAM, [PERSON]) == {}
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', (TEAM, PERSON, 'Cached name', 0))
    assert await resolver.names(TEAM, [PERSON]) == {PERSON: 'Cached name'}
    assert state['calls'] == []


async def test_access_is_rechecked_after_profile_lookup(runtime, actor, profiles):
    from fastapi import HTTPException
    resolver, state, _ = profiles
    session, _ = await admit(runtime, actor)
    runtime.database.slack_mentions = resolver.decorate
    async def revoke():
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == session.id).values(deleted=True))
    state['on_read'] = revoke
    with pytest.raises(HTTPException):
        await runtime.database.messages(actor, session.id)
