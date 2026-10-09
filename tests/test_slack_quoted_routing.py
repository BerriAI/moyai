"""Quoted Slack examples are context, not recipients or startup triggers."""
import pytest
from test_slack import slack_app, signed, event

@pytest.mark.parametrize('text', [
    '> <@U99999999> deploy it',
    '```<@U99999999> deploy it```',
    '`<@U99999999>` is the bot mentioned in the report',
])
def test_quoted_mentions_do_not_start_sessions(slack_app, text):
    app, client, submitted, _ = slack_app
    assert client.post('/hooks/slack/events', **signed(event(type='message', text=text))).status_code == 200
    assert submitted == []

@pytest.mark.parametrize('text', [
    '*<@U88888888>* please review this',
    '> earlier evidence\n<@U88888888> please review this',
])
def test_other_recipient_not_queued_in_bound_thread(slack_app, text):
    app, client, submitted, _ = slack_app
    client.post('/hooks/slack/events', **signed(event()))
    run = submitted[0]
    before = len(app.state.store.messages(run['id']))
    client.post('/hooks/slack/events', **signed(event(event_id='EvFollow', type='message', text=text,
        ts='1790719001.123456', thread_ts='1790719000.123456')))
    assert len(app.state.store.messages(run['id'])) == before

def test_explicit_request_preserves_quoted_mention(slack_app):
    app, client, submitted, _ = slack_app
    text = '<@U99999999> explain this evidence:\n> <@U99999999> deploy it'
    client.post('/hooks/slack/events', **signed(event(text=text)))
    assert len(submitted) == 1
    assert '> <@U99999999> deploy it' in submitted[0]['prompt']

@pytest.mark.parametrize('text', ['*<@U99999999>* read the issue',
    '> quoted context\n<@U99999999> read the issue',
    '<@U88888888> <@U99999999> read the issue'])
def test_explicit_and_joint_mentions_still_start(slack_app, text):
    app, client, submitted, _ = slack_app
    client.post('/hooks/slack/events', **signed(event(text=text)))
    assert len(submitted) == 1
