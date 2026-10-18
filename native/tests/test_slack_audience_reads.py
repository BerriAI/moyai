"""Provider-call volume, retry pacing and isolation for browser audience reads."""

import asyncio
from types import SimpleNamespace

import pytest
from app.connector_errors import ConnectorError

from moyai.slack_audience_reads import AUDIENCE_TTL, SlackAudienceReads


@pytest.fixture
def provider():
    state = SimpleNamespace(now=0, calls=[], failure=None, hold=None)
    async def token():
        return 'synthetic-token'
    async def request(method, url, **kwargs):
        state.calls.append((url, kwargs['params'].copy()))
        if state.hold:
            await state.hold.wait()
        if state.failure:
            raise state.failure
        return {'ok': True, 'channel': {'id': kwargs['params']['channel'], 'is_private': True,
                'is_channel': True, 'is_ext_shared': False, 'topic': 'do not cache content'},
                'members': ['U1'], 'response_metadata': {'next_cursor': ''}}
    slack = SimpleNamespace(connectors=SimpleNamespace(slack_bot_token=token, request=request))
    return SimpleNamespace(state=state, slack=slack, reads=SlackAudienceReads(clock=lambda: state.now),
                           installed={'team_id': 'T1', 'authority': 'a'})


async def test_concurrent_readers_share_one_lookup_and_cancellation_does_not_cancel_peers(provider):
    p = provider
    p.state.hold = asyncio.Event()
    readers = [asyncio.create_task(p.reads.read(p.slack, p.installed, 'conversations.info', 'C1'))
               for _ in range(40)]
    while not p.state.calls:
        await asyncio.sleep(0)
    readers[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await readers[0]
    p.state.hold.set()
    results = await asyncio.gather(*readers[1:])
    assert all(result['channel']['id'] == 'C1' for result in results)
    assert len(p.state.calls) == 1
    assert 'topic' not in results[0]['channel']
    assert not p.reads.pending
    p.state.now = AUDIENCE_TTL
    await p.reads.read(p.slack, p.installed, 'conversations.info', 'C1')
    assert len(p.state.calls) == 2


async def test_snapshots_are_isolated_by_installation_workspace_channel_method_and_cursor(provider):
    p = provider
    queries = [
        (p.installed, 'conversations.info', 'C1', ''),
        ({'team_id': 'T1', 'authority': 'b'}, 'conversations.info', 'C1', ''),
        ({'team_id': 'T2', 'authority': 'a'}, 'conversations.info', 'C1', ''),
        (p.installed, 'conversations.info', 'C2', ''),
        (p.installed, 'conversations.members', 'C1', ''),
        (p.installed, 'conversations.members', 'C1', 'next'),
    ]
    for installed, method, channel, cursor in queries * 2:
        await p.reads.read(p.slack, installed, method, channel, cursor)
    assert len(p.state.calls) == len(queries)


async def test_rate_limit_honors_delay_across_channels_without_blocking_other_methods(provider):
    p = provider
    p.state.failure = ConnectorError('limited', code='rate_limited', status_code=429,
                                    retryable=True, retry_after=10)
    with pytest.raises(ConnectorError):
        await p.reads.read(p.slack, p.installed, 'conversations.members', 'C1')
    p.state.failure = None
    p.state.now = 4
    for channel in ('C1', 'C2', 'C3'):
        with pytest.raises(ConnectorError) as limited:
            await p.reads.read(p.slack, p.installed, 'conversations.members', channel)
        assert limited.value.retry_after == 6
    assert len(p.state.calls) == 1
    await p.reads.read(p.slack, p.installed, 'conversations.info', 'C1')
    p.state.now = 10
    assert (await p.reads.read(p.slack, p.installed, 'conversations.members', 'C1'))['members'] == ['U1']
    assert len(p.state.calls) == 3


async def test_expired_snapshot_is_not_returned_when_refresh_fails(provider):
    p = provider
    await p.reads.read(p.slack, p.installed, 'conversations.members', 'C1')
    p.state.now = AUDIENCE_TTL
    p.state.failure = TimeoutError('synthetic failure')
    with pytest.raises(TimeoutError):
        await p.reads.read(p.slack, p.installed, 'conversations.members', 'C1')
    assert not p.reads.snapshots
