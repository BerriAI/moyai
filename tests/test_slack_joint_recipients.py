"""Joint addressees must be recognized without treating subjects as recipients."""
import pytest
from test_slack import slack_app, signed, event

@pytest.mark.parametrize('prefix,accept', [
    ('<@U88888888> and <@U99999999>', True),
    ('<@U88888888> & <@U99999999>', True),
    ('<@U88888888> &amp; <@U99999999>', True),
    ('Hi <@U88888888>, and <@U99999999>', True),
    ('<@U88888888>, <@U99999999>', True),
    ('<@U88888888> please relay to <@U99999999>', False),
    ('<@U88888888> and ask about <@U99999999>', False),
    ('<@U88888888> and `<@U99999999>`', False),
])
@pytest.mark.parametrize('bound', [False, True])
def test_joint_recipient_routing(slack_app, prefix, accept, bound):
    app, client, runs, _ = slack_app
    if bound:
        assert client.post('/hooks/slack/events', **signed(event())).status_code == 200
        app.state.store.claim_message(runs[0]['id'])
        app.state.store.update_run(runs[0]['id'], status='running')
    before = len(app.state.store.rows('SELECT id FROM messages'))
    payload = event(event_id='EvJoint', text=prefix + ' please correct the host.',
        ts='1790719001.123456', thread_ts='1790719000.123456')
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(app.state.store.rows('SELECT id FROM messages')) - before == int(accept)
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(app.state.store.rows('SELECT id FROM messages')) - before == int(accept)
