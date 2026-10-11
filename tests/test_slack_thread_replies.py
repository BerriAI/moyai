"""Conversational follow-ups use the signed webhook and durable input queue."""
import pytest

from app.db import Store
from test_slack import slack_app, signed
from test_slack_chat import finish, send, start


@pytest.mark.parametrize('state', ['active', 'idle'])
@pytest.mark.parametrize('user', ['U12345678', 'U87654321'])
def test_unmentioned_reply_is_saved_once_in_the_existing_session(slack_app, state, user):
    app, client, run_id = start(slack_app)
    store = app.state.store
    if state == 'active':
        store.claim_message(run_id)
        store.update_run(run_id, status='running')
    else:
        finish(app, run_id, 'Would you like a list of steps?')
    before = len(store.messages(run_id))
    text = 'Sure. Give me a list of steps to do'
    payload = send(client, 1, text, user=user)
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    # Slack may redeliver the same physical message with a different event ID.
    payload['event_id'] = 'EvRedeliveredReply'
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    messages = store.messages(run_id)
    assert len(messages) == before + 1
    assert messages[-1]['input_kind'] == 'observation'
    assert messages[-1]['status'] == 'queued'
    assert len(store.rows('SELECT id FROM runs')) == 1
    assert not store.rows("SELECT message_id FROM slack_receipts WHERE event_id='EvChat1'")
    versions = app.state.slack.sources.read(store.run(run_id), {})['entries']
    observed = [e for e in versions if e['message'].get('text') == text]
    assert len(observed) == 1 and observed[0]['message']['user'] == user



@pytest.mark.parametrize('text,accepted', [
    ('<@U88888888> can you look at this?', False),
    ('Can you look at this, <@U88888888>?', False),
    ('Hey *<@U88888888>*, please take over', False),
    ('cc <@W88888888|teammate> please take over', False),
    ('<@U88888888> please help\n> <@U99999999> quoted request', False),
    ('<@U88888888> please help; `<@U99999999>` is only an example', False),
    ('<@U88888888> and <@U99999999> please both help', True),
    ('<@U88888888> please help; <@U99999999> list the steps', True),
    ('Please list the steps, cc <@U88888888>. <@U99999999>', True),
    ('Explain this example: `<@U88888888>`', False),
    ('Please check this:\n> <@U88888888> original question', False),
])
def test_only_authored_mentions_route_existing_thread_replies(slack_app, text, accepted):
    app, client, run_id = start(slack_app)
    before = len(app.state.store.messages(run_id))
    send(client, 1, text)
    assert len(app.state.store.messages(run_id)) == before + 1
    message = app.state.store.messages(run_id)[-1]
    assert message['input_kind'] == ('user' if accepted else 'observation')
    assert bool(app.state.store.rows("SELECT 1 FROM slack_receipts WHERE event_id='EvChat1'")) == accepted


def test_unmentioned_reply_survives_reopening_the_store(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Would you like a list of steps?')
    payload = send(client, 1, 'Sure. Give me a list of steps to do')
    restored = Store(app.state.settings.data_dir)
    message = restored.claim_message(run_id)
    assert message and message['input_kind'] == 'observation'
    from app.slack_sources import SlackSources
    versions = SlackSources(restored).read(restored.run(run_id), {})['entries']
    assert versions[-1]['message']['text'] == 'Sure. Give me a list of steps to do'
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(restored.messages(run_id)) == 3
