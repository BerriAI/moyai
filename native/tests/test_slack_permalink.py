from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from moyai.contracts import NewMessage, NewSession
from moyai.inbound_events import InboundEvent
from moyai.slack_permalink import permalink


@pytest.mark.parametrize('url,valid', [
    ('https://workspace.slack.com/archives/CTEST001/p1792255741001?thread_ts=x#fragment', True),
    ('https://workspace.slack.com.evil.test/archives/CTEST001/p1792255741001', False),
    ('https://workspace.slack.com/archives/COTHER/p1792255741001', False),
    ('https://secret@workspace.slack.com/archives/CTEST001/p1792255741001', False),
    ('https://workspace.slack.com:1234/archives/CTEST001/p1792255741001', False),
    ('https://[invalid', False),
])
@pytest.mark.parametrize('app', [None, 'dot'])
async def test_source_link_validates_destination_and_never_returns_credentials(runtime, actor, url, valid, app):
    channel = 'DTEST001' if app == 'dot' else 'CTEST001'
    url = url.replace('CTEST001', channel)
    session = await runtime.database.create_session(actor, NewSession())
    body = NewMessage.from_event(InboundEvent(
        source='slack', sender_id=actor.id, deduplication_key='permalink', text='Original message',
        provenance={'team_id': 'TTEST001', 'channel_id': channel,
                    'thread_ts': '1792255740.000', 'message_ts': '1792255741.001', 'user_id': 'UTEST001',
                    **({'slack_app': app} if app else {})},
    ))
    await runtime.admission.submit(actor, session.id, body)
    message = (await runtime.database.messages(actor, session.id))[0]

    async def dev_token():
        return 'dev-token'

    async def dot_token():
        return 'dot-token'

    async def request(method, endpoint, *, headers, params):
        assert (method, endpoint) == ('GET', 'https://slack.com/api/chat.getPermalink')
        assert params == {'channel': channel, 'message_ts': '1792255741.001'}
        assert headers == {'Authorization': f'Bearer {app or "dev"}-token'}
        return {'ok': True, 'permalink': url}

    dot = SimpleNamespace(installation=lambda: {'team_id': 'TTEST001'}, bot_token=dot_token,
                          connectors=SimpleNamespace(request=request))
    runtime.product_slack = SimpleNamespace(installation=lambda: {'team_id': 'TTEST001'},
        bot_token=dev_token, connectors=SimpleNamespace(request=request),
        for_app=lambda saved: {'dev': runtime.product_slack, 'dot': dot}[saved])
    try:
        result = await permalink(runtime, actor, session.id, message['id'])
        assert result == (f'https://workspace.slack.com/archives/{channel}/p1792255741001' if valid else
                          f'https://slack.com/app_redirect?team=TTEST001&channel={channel}')
        with pytest.raises(HTTPException):
            await permalink(runtime, actor, 'missing-session', message['id'])
    finally:
        runtime.product_slack = None
