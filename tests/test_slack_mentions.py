import asyncio
import time

import pytest
from app.db import Store
from app.slack_mentions import MAX_MENTIONS
from test_identities import profiles, install
from test_slack import slack_app, signed, event
from test_slack_chat import start, finish, send
from test_spend import sign_in


TEAM, TARGET = 'T12345678', 'U87654321'


def mention_run(app, text=None):
    return app.state.store.create_slack_run('EvMention', text or f'cc: <@{TARGET}> can you help?', [],
        'C12345678', '1790719000.123456', 'U12345678', team_id=TEAM)


def test_cc_uses_mentioned_profile_not_sender_or_owner_and_preserves_raw_input(slack_app):
    app, client, run_id = start(slack_app)
    store = app.state.store
    store.execute("UPDATE users SET name='Original owner' WHERE kind='slack'")
    with store.connect() as conn:
        target = store.slack_identity_in(conn, TEAM, TARGET)
    store.execute('UPDATE users SET name=?,email=? WHERE id=?', ('Tin Lo', 'tin@example.com', target))
    finish(app, run_id, 'Ready.')
    text = f'cc: <@{TARGET}> and <@{TARGET}|old-name> can you help? Literal {TARGET}.'
    send(client, 1, '<@U99999999> ' + text, user='U11111111')
    store.execute("UPDATE users SET name='Mateo' WHERE id='slack:T12345678:U11111111'")
    sign_in(app, client)
    reply = client.get(f'/api/runs/{run_id}').json()['messages'][-1]
    assert reply['display_content'] == f'Slack reply from Mateo:\ncc: @Tin Lo and @Tin Lo can you help? Literal {TARGET}.'
    assert reply['content'] == f'Slack reply from U11111111:\n{text}'
    assert reply['user_id'] == 'slack:T12345678:U11111111'
    assert store.run(run_id)['owner_id'] == 'slack:T12345678:U12345678'
    assert store.claim_message(run_id)['content'] == reply['content']
    store.execute('UPDATE users SET name=? WHERE id=?', ('Tin Updated', target))
    assert '@Tin Updated' in store.messages(run_id)[-1]['display_content']


def test_only_slack_inputs_expand_mentions_including_legacy_initial_history(profiles):
    app, control = profiles
    run = mention_run(app)
    store = app.state.store
    store.execute("DELETE FROM slack_message_mentions")
    store.execute("DELETE FROM slack_mention_names")
    # Reopening an older database discovers existing messages without a replay.
    restored = Store(app.state.settings.data_dir)
    assert 'display_content' not in restored.messages(run['id'])[0]
    control['profile'].update(id=TARGET, profile={'real_name': 'Tin Lo'})
    asyncio.run(restored.slack_mentions.sync_due(app.state.connectors))
    assert restored.messages(run['id'])[0]['display_content'] == 'cc: @Tin Lo can you help?'
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], f'An example: <@{TARGET}>')
    actor = store.identity({'method': 'google', 'identity': {'sub': 'web', 'email': 'web@example.com', 'name': 'Web user'}})
    store.enqueue_message(run['id'], f'Web example: <@{TARGET}>', 'web-example', user_id=actor)
    history = restored.messages(run['id'])
    assert 'display_content' not in history[-2] and 'display_content' not in history[-1]
    assert control['calls'] == [TARGET]


def test_initial_slack_event_registers_mentions_without_waiting_for_lookup(slack_app):
    app, client, runs, _ = slack_app
    payload = event(text=f'<@U99999999> cc: <@{TARGET}> can you help?')
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(runs) == 1
    assert len(app.state.store.rows('SELECT * FROM slack_message_mentions')) == 1
    app.state.store.execute('UPDATE slack_mention_names SET name=?', ('Tin Lo',))
    assert app.state.store.messages(runs[0]['id'])[0]['display_content'] == 'cc: @Tin Lo can you help?'


def test_mention_lookup_uses_bot_without_creating_or_linking_an_identity(profiles):
    app, control = profiles
    install(app, 'users:read')
    app.state.settings.slack_identity_linking_enabled = False
    run = mention_run(app)
    control['profile'].update(id=TARGET, profile={'display_name': 'Tin Lo', 'email': 'ignored@example.com'})
    asyncio.run(app.state.identities.sync_due())
    assert control['calls'] == [TARGET]
    assert app.state.store.messages(run['id'])[0]['display_content'] == 'cc: @Tin Lo can you help?'
    assert not app.state.store.rows('SELECT * FROM users WHERE id=?', (f'slack:{TEAM}:{TARGET}',))
    assert not app.state.store.rows('SELECT * FROM identity_audit')
    assert app.state.store.events(run['id'])[-1]['message'] == 'Slack mention names updated'
    asyncio.run(app.state.identities.sync_due())
    assert control['calls'] == [TARGET]  # Cached; normal chat reads never call Slack.


@pytest.mark.parametrize('failure', ['api', 'id', 'team', 'malformed', 'empty'])
def test_failed_or_mismatched_profiles_keep_original_mention_and_back_off(profiles, failure):
    app, control = profiles
    run = mention_run(app, f'cc: <@{TARGET}|do-not-trust-this-label>')
    control['profile'].update(id=TARGET, profile={'real_name': 'Tin Lo'})
    if failure == 'api':
        control['fail'] = True
    elif failure == 'id':
        control['profile']['id'] = 'U11111111'
    elif failure == 'team':
        control['profile']['team_id'] = 'T87654321'
    elif failure == 'empty':
        control['profile']['profile'] = {}
    else:
        control['profile']['profile'] = 'invalid'
    asyncio.run(app.state.store.slack_mentions.sync_due(app.state.connectors))
    assert 'display_content' not in app.state.store.messages(run['id'])[0]
    assert app.state.store.rows('SELECT next_check FROM slack_mention_names')[0]['next_check'] > time.time()
    asyncio.run(app.state.store.slack_mentions.sync_due(app.state.connectors))
    assert control['calls'] == [TARGET]


@pytest.mark.parametrize('blocked', ['scope', 'policy', 'other-team', 'disconnect-during-lookup'])
def test_lookup_requires_same_workspace_bot_permission(profiles, blocked):
    app, control = profiles
    run = mention_run(app)
    if blocked == 'scope':
        install(app, 'reactions:write')
    elif blocked == 'policy':
        app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    elif blocked == 'other-team':
        app.state.store.execute("UPDATE slack_mention_names SET team_id='T87654321'")
    else:
        original = app.state.connectors.request

        async def request(*args, **kwargs):
            result = await original(*args, **kwargs)
            install(app, 'reactions:write')
            return result

        app.state.connectors.request = request
        control['profile'].update(id=TARGET, profile={'real_name': 'Tin Lo'})
    asyncio.run(app.state.store.slack_mentions.sync_due(app.state.connectors))
    assert control['calls'] == ([TARGET] if blocked == 'disconnect-during-lookup' else [])
    assert 'display_content' not in app.state.store.messages(run['id'])[0]


@pytest.mark.parametrize('failure', ['api', 'empty'])
def test_workspace_scoping_retry_and_name_refresh(profiles, failure):
    app, control = profiles
    run = mention_run(app)
    store = app.state.store
    store.execute('INSERT INTO slack_mention_names VALUES(?,?,?,?)', ('T87654321', TARGET, 'Wrong team', 0))
    control['profile'].update(id=TARGET, profile={'real_name': 'Tin Lo'})
    asyncio.run(store.slack_mentions.sync_due(app.state.connectors))
    control['fail'] = failure == 'api'
    if failure == 'empty':
        control['profile']['profile'] = {}
    store.execute('UPDATE slack_mention_names SET next_check=0 WHERE team_id=?', (TEAM,))
    asyncio.run(store.slack_mentions.sync_due(app.state.connectors))
    assert store.messages(run['id'])[0]['display_content'] == 'cc: @Tin Lo can you help?'
    control['fail'] = False
    control['profile']['profile']['real_name'] = 'Tin Updated'
    store.execute('UPDATE slack_mention_names SET next_check=0 WHERE team_id=?', (TEAM,))
    asyncio.run(store.slack_mentions.sync_due(app.state.connectors))
    assert store.messages(run['id'])[0]['display_content'] == 'cc: @Tin Updated can you help?'
    assert control['calls'] == [TARGET] * 3


def test_deleted_messages_do_not_trigger_profile_lookups(profiles):
    app, control = profiles
    run = mention_run(app)
    app.state.store.execute("UPDATE messages SET status='deleted' WHERE run_id=?", (run['id'],))
    asyncio.run(app.state.store.slack_mentions.sync_due(app.state.connectors))
    assert not control['calls']


def test_mentions_are_bounded_deduplicated_and_allow_w_ids(profiles):
    app, _ = profiles
    run = mention_run(app, ' '.join(f'<@W{index:08d}>' for index in range(80)) + ' <@Ubad>')
    store = app.state.store
    store.messages(run['id'])
    store.messages(run['id'])
    assert len(store.rows('SELECT * FROM slack_mention_names')) == MAX_MENTIONS
    assert len(store.rows('SELECT * FROM slack_message_mentions')) == MAX_MENTIONS
