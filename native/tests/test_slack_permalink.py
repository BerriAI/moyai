from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from moyai.contracts import NewMessage, NewSession
from moyai.slack_permalink import permalink


@pytest.mark.parametrize('url,valid', [
    ('https://workspace.slack.com/archives/CTEST001/p1792255741001?thread_ts=x#fragment', True),
    ('https://workspace.slack.com.evil.test/archives/CTEST001/p1792255741001', False),
    ('https://workspace.slack.com/archives/COTHER/p1792255741001', False),
    ('https://secret@workspace.slack.com/archives/CTEST001/p1792255741001', False),
    ('https://workspace.slack.com:1234/archives/CTEST001/p1792255741001', False),
    ('https://[invalid', False),
])
async def test_source_link_validates_destination_and_never_returns_credentials(runtime, actor, url, valid):
    session = await runtime.database.create_session(actor, NewSession())
    body = NewMessage(prompt='Original message', request_key='permalink')
    body._source = {'kind': 'slack', 'team_id': 'TTEST001', 'channel_id': 'CTEST001',
                    'thread_ts': '1792255740.000', 'message_ts': '1792255741.001', 'user_id': 'UTEST001'}
    await runtime.database.submit(actor, session.id, body)
    message = (await runtime.database.messages(actor, session.id))[0]
    async def token():
        return 'synthetic-token'
    async def request(*args, **kwargs):
        return {'ok': True, 'permalink': url}
    runtime.product_slack = SimpleNamespace(installation=lambda: {'team_id': 'TTEST001'},
        connectors=SimpleNamespace(slack_bot_token=token, request=request))
    try:
        result = await permalink(runtime, actor, session.id, message['id'])
        assert result == ('https://workspace.slack.com/archives/CTEST001/p1792255741001' if valid else
                          'https://slack.com/app_redirect?team=TTEST001&channel=CTEST001')
        with pytest.raises(HTTPException):
            await permalink(runtime, actor, 'missing-session', message['id'])
    finally:
        runtime.product_slack = None
