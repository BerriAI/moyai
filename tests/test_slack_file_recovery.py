"""Bounded read recovery; controlled HTTP is supplementary, not live Slack proof."""
import asyncio
import gzip
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import httpx
import pytest

from app import slack_files
from app.connector_errors import ConnectorError
from app.connectors import Connectors
from app.slack_files import SlackFileError, SlackFiles
from test_slack import event, signed, slack_app
from test_slack_files import FILE, provider


TEAM = 'T12345678'
CONTENT = b'Verified attachment bytes.'
PRIVATE = 'private-response xoxb-secret https://files.slack.com/files-pri/private'


def metadata(size=len(CONTENT), **extra):
    return {'ok': True, 'file': {
        'id': FILE['id'], 'name': 'note.txt', 'mimetype': 'text/plain', 'size': size,
        'url_private_download': 'https://files.slack.com/files-pri/private', **extra}}


@pytest.fixture
def reader(monkeypatch):
    connectors = SimpleNamespace(
        slack_installation=lambda: {'team_id': TEAM},
        slack_bot_token=AsyncMock(return_value='xoxb-secret'),
        request=AsyncMock(return_value=metadata()))
    owner = SimpleNamespace(connectors=connectors,
        settings=SimpleNamespace(attachment_storage_limit_mb=1),
        status=lambda: {'enabled': True})
    files = SlackFiles(owner)
    monkeypatch.setattr(slack_files, 'RETRY_DELAY_SECONDS', .001)
    return files, connectors


def downloads(monkeypatch, *responses):
    calls, handles = [], []
    original_file = slack_files.tempfile.TemporaryFile

    def temporary():
        raw = original_file()
        handles.append(raw)
        return raw

    class Response:
        def __init__(self, response):
            self.status = response.get('status', 200)
            self.headers = response.get('headers', {})
            self.chunks = response.get('chunks', [CONTENT])
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            for chunk in self.chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                if isinstance(chunk, asyncio.Event):
                    await chunk.wait()
                else:
                    yield chunk

    class Session:
        def __init__(self, **kwargs):
            assert kwargs['auto_decompress'] is False
            timeout = kwargs['timeout']
            assert timeout.total == 45 and timeout.sock_read == 15 and timeout.sock_connect == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, *, headers, allow_redirects):
            assert url == 'https://files.slack.com/files-pri/private'
            assert headers == {'Authorization': 'Bearer xoxb-secret', 'Accept-Encoding': 'identity'}
            assert allow_redirects is False
            response = responses[min(len(calls), len(responses)-1)]
            calls.append(url)
            return Response(response)

    monkeypatch.setattr(slack_files.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(slack_files.tempfile, 'TemporaryFile', temporary)
    return calls, handles


@pytest.mark.parametrize('first', [
    {'headers': {'Content-Length': str(len(CONTENT))}},
    {'chunks': [CONTENT]},
])
async def test_stale_size_gets_fresh_metadata_and_exact_complete_bytes(reader, monkeypatch, first):
    files, connectors = reader
    connectors.request.side_effect = [metadata(1), metadata()]
    calls, handles = downloads(monkeypatch, first, {})
    _, raw, inspected = await files.read(FILE['id'], TEAM)
    try:
        assert raw.read() == CONTENT and inspected[2] == CONTENT.decode()
        assert len(calls) == connectors.request.await_count == connectors.slack_bot_token.await_count == 2
        assert all(handle.closed for handle in handles[:-1])
    finally:
        raw.close()


@pytest.mark.parametrize('failure,code', [
    (aiohttp.ClientPayloadError(PRIVATE), 'truncated_stream'),
    (aiohttp.ClientConnectionError(PRIVATE), 'transport'),
    (TimeoutError(PRIVATE), 'timeout'),
])
async def test_interrupted_download_restarts_from_zero_and_closes_prior_file(reader, monkeypatch, failure, code):
    files, connectors = reader
    calls, handles = downloads(monkeypatch, {'chunks': [b'Verified ', failure]}, {})
    _, raw, _ = await files.read(FILE['id'], TEAM)
    try:
        assert raw.read() == CONTENT
        assert len(calls) == connectors.request.await_count == 2
        assert handles[0].closed
    finally:
        raw.close()


@pytest.mark.parametrize('response,code', [
    ({'chunks': [b'short']}, 'metadata_size_mismatch'),
    ({'chunks': [CONTENT, b'extra']}, 'metadata_size_mismatch'),
    ({'chunks': [b'short', aiohttp.ClientPayloadError(PRIVATE)]}, 'truncated_stream'),
    ({'headers': {'Content-Encoding': 'gzip'}}, 'encoded_response'),
    ({'headers': {'Content-Length': 'private-header'}}, 'invalid_headers'),
    ({'status': 206}, 'partial_response'),
    ({'headers': {'Content-Range': 'bytes 0-3/100'}}, 'partial_response'),
    ({'status': 503}, 'transient_http'),
])
async def test_unverifiable_read_stops_after_two_attempts_with_safe_detail(reader, monkeypatch, response, code):
    files, connectors = reader
    calls, handles = downloads(monkeypatch, response)
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    detail = caught.value.diagnostic()
    assert detail['code'] == code and detail['attempts'] == 2
    assert detail['declared_bytes'] == len(CONTENT)
    assert len(calls) == connectors.request.await_count == 2
    assert all(handle.closed for handle in handles)
    for private in ('xoxb-secret', 'https://', 'private-header', 'private-response', CONTENT.decode()):
        assert private not in str(caught.value) + json.dumps(detail)
    assert detail['remaining_blocker'] and detail['next_action']


@pytest.mark.parametrize('header,encoding', [
    (None, 'identity'), ('identity', 'identity'), (' GZip ', 'gzip'),
    ('br', 'br'), ('deflate', 'deflate'), ('zstd', 'zstd'),
    ('gzip, br', 'unsupported'), ('', 'unsupported'), (PRIVATE, 'unsupported'),
])
async def test_failure_representation_is_allowlisted_and_header_rejection_has_no_byte_count(
        reader, monkeypatch, header, encoding):
    files, _ = reader
    headers = {} if header is None else {'Content-Encoding': header}
    downloads(monkeypatch, {'headers': headers, 'chunks': [b'x']})
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    detail = caught.value.diagnostic()
    assert detail['content_encoding'] == encoding
    assert detail['content_encoding_present'] is (header is not None)
    assert detail['attempts'] == 2
    assert all(attempt['content_encoding'] == encoding for attempt in detail['attempt_history'])
    assert f'content_encoding={encoding}' in str(caught.value)
    if encoding == 'identity':
        assert detail['code'] == 'metadata_size_mismatch' and detail['received_bytes'] == 1
    else:
        assert detail['code'] == 'encoded_response' and detail['stage'] == 'headers'
        assert 'received_bytes' not in detail
        assert all('received_bytes' not in attempt for attempt in detail['attempt_history'])
    assert PRIVATE not in json.dumps(detail) + str(caught.value)


@pytest.mark.parametrize('failure', ['first_read', 'storage', 'empty_body'])
async def test_missing_byte_observation_is_not_reported_as_zero(reader, monkeypatch, failure):
    files, _ = reader
    downloads(monkeypatch, {'chunks': [aiohttp.ClientPayloadError(PRIVATE)] if failure == 'first_read' else []})
    if failure == 'storage':
        def unavailable():
            raise OSError(PRIVATE)
        monkeypatch.setattr(slack_files.tempfile, 'TemporaryFile', unavailable)
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    detail = caught.value.diagnostic()
    assert detail['content_encoding'] == 'identity'
    assert detail['content_encoding_present'] is False
    if failure == 'empty_body':
        assert detail['code'] == 'metadata_size_mismatch' and detail['received_bytes'] == 0
    else:
        assert detail['code'] == ('storage' if failure == 'storage' else 'truncated_stream')
        assert 'received_bytes' not in detail
        assert all('received_bytes' not in attempt for attempt in detail['attempt_history'])


@pytest.mark.parametrize('response,code', [
    ({'status': 401}, 'authentication'),
    ({'status': 403}, 'authentication'),
    ({'status': 302, 'headers': {'Location': 'https://evil.example/private'}}, 'redirect'),
    ({'status': 404}, 'unavailable'),
    ({'headers': {'Content-Length': str(1024*1024+1)}}, 'hard_limit'),
])
async def test_permanent_header_failures_do_not_retry_or_read_body(reader, monkeypatch, response, code):
    files, connectors = reader
    response['chunks'] = [AssertionError('The rejected response body must not be read.')]
    calls, handles = downloads(monkeypatch, response)
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == code and caught.value.diagnostic()['attempts'] == 1
    assert len(calls) == connectors.request.await_count == 1
    assert not handles


async def test_stream_actual_byte_limit_is_independent_of_content_length(reader, monkeypatch):
    files, connectors = reader
    connectors.request.return_value = metadata(1024*1024)
    calls, handles = downloads(monkeypatch, {'chunks': [*[b'x'*(64*1024)]*16, b'overflow']})
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == 'hard_limit'
    assert caught.value.diagnostic()['received_bytes'] == 1024*1024+8
    assert len(calls) == 1 and handles[0].closed


@pytest.mark.parametrize('failure,code,retries', [
    (ConnectorError(PRIVATE, code='invalid_auth'), 'authentication', 1),
    (ConnectorError(PRIVATE, code='missing_scope'), 'missing_scope', 1),
    (ConnectorError(PRIVATE, code='file_not_found'), 'unavailable', 1),
    (ConnectorError(PRIVATE, status_code=503, retryable=True), 'transport', 2),
])
async def test_metadata_errors_are_classified_without_copying_provider_text(reader, monkeypatch, failure, code, retries):
    files, connectors = reader
    connectors.request.side_effect = failure
    calls, handles = downloads(monkeypatch, {})
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == code
    assert caught.value.diagnostic()['stage'] == 'metadata'
    assert connectors.request.await_count == retries
    assert not calls and not handles
    assert PRIVATE not in str(caught.value)
    assert 'content_encoding' not in caught.value.diagnostic()
    assert 'content_encoding_present' not in caught.value.diagnostic()
    assert 'received_bytes' not in caught.value.diagnostic()


@pytest.mark.parametrize('at', ['metadata', 'download'])
async def test_retry_after_is_observed_before_fresh_request(reader, monkeypatch, at):
    files, connectors = reader
    loop = asyncio.get_running_loop()
    requests = []
    monkeypatch.setattr(slack_files.random, 'uniform',
                        lambda *args: pytest.fail('Retry-After must never be jittered.'))

    async def info(*args, **kwargs):
        requests.append(loop.time())
        if len(requests) == 1 and at == 'metadata':
            raise ConnectorError(PRIVATE, status_code=429, retry_after=.03)
        return metadata()

    connectors.request.side_effect = info
    calls, _ = downloads(monkeypatch,
        *([{'status': 429, 'headers': {'Retry-After': '.03'}}, {}] if at == 'download' else [{}]))
    _, raw, _ = await files.read(FILE['id'], TEAM)
    raw.close()
    assert len(requests) == 2 and requests[1]-requests[0] >= .03
    assert len(calls) == (2 if at == 'download' else 1)


@pytest.mark.parametrize('endpoint', ['minimum', 'maximum'])
async def test_non_rate_retry_uses_bounded_jitter_and_still_refreshes_metadata(reader, monkeypatch, endpoint):
    files, connectors = reader
    loop = asyncio.get_running_loop()
    requests, bounds = [], []
    monkeypatch.setattr(slack_files, 'RETRY_DELAY_SECONDS', .02)

    def jitter(low, high):
        bounds.append((low, high))
        return low if endpoint == 'minimum' else high

    async def info(*args, **kwargs):
        requests.append(loop.time())
        return metadata()

    monkeypatch.setattr(slack_files.random, 'uniform', jitter)
    connectors.request.side_effect = info
    downloads(monkeypatch, {'status': 503}, {})
    _, raw, _ = await files.read(FILE['id'], TEAM)
    raw.close()
    assert bounds == [(.01, .03)]
    assert len(requests) == 2
    assert requests[1]-requests[0] >= (.01 if endpoint == 'minimum' else .03)


@pytest.mark.parametrize('interval', [None, 'invalid', 'NaN', '-1', '99999'])
async def test_unusable_or_over_budget_rate_limit_never_retries_early(reader, monkeypatch, interval):
    files, connectors = reader
    headers = {} if interval is None else {'Retry-After': interval}
    calls, _ = downloads(monkeypatch, {'status': 429, 'headers': headers})
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == 'rate_limited' and caught.value.diagnostic()['attempts'] == 1
    assert len(calls) == connectors.request.await_count == 1
    assert 'no early retry' in str(caught.value).lower()
    if interval == '99999':
        # A later file must also respect this reader's known cooldown.
        with pytest.raises(SlackFileError, match='rate_limited'):
            await files.read(FILE['id'], TEAM)
        assert len(calls) == connectors.request.await_count == 1


async def test_read_budget_bounds_a_stalled_stream_and_closes_file(reader, monkeypatch):
    files, connectors = reader
    monkeypatch.setattr(slack_files, 'READ_BUDGET_SECONDS', .03)
    calls, handles = downloads(monkeypatch, {'chunks': [b'a', asyncio.Event()]})
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == 'read_budget'
    assert caught.value.diagnostic()['received_bytes'] == 1
    assert len(calls) == connectors.request.await_count == 1 and handles[0].closed


async def test_cancellation_is_not_a_retry_and_closes_file(reader, monkeypatch):
    files, connectors = reader
    calls, handles = downloads(monkeypatch, {'chunks': [b'a', asyncio.Event()]})
    task = asyncio.create_task(files.read(FILE['id'], TEAM))
    async with asyncio.timeout(2):
        while not handles:
            await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == connectors.request.await_count == 1 and handles[0].closed


async def test_cancellation_drains_inspection_before_closing_file(reader, monkeypatch):
    files, _ = reader
    _, handles = downloads(monkeypatch, {})
    entered, release = threading.Event(), threading.Event()

    def inspect(raw):
        entered.set()
        assert release.wait(3)
        assert not raw.closed
        return 'text/plain', b'', raw.read().decode()

    monkeypatch.setattr(slack_files, 'inspect_file', inspect)
    task = asyncio.create_task(files.read(FILE['id'], TEAM))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not handles[0].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles[0].closed


async def test_access_revocation_before_retry_does_not_fetch_again(reader, monkeypatch):
    files, connectors = reader
    calls, _ = downloads(monkeypatch, {'chunks': [b'wrong']})
    original = connectors.request

    async def info(*args, **kwargs):
        result = await original(*args, **kwargs)
        # First post-metadata access check is allowed; the retry must recheck.
        checks = 0
        def installed():
            nonlocal checks
            checks += 1
            return {'team_id': TEAM if checks == 1 else 'T87654321'}
        connectors.slack_installation = installed
        return result

    connectors.request = info
    with pytest.raises(SlackFileError) as caught:
        await files.read(FILE['id'], TEAM)
    assert caught.value.code == 'access_changed'
    assert len(calls) == original.await_count == 1
    assert caught.value.diagnostic()['attempts'] == 2


@pytest.mark.parametrize('first', ['encoded', 'truncated'])
async def test_real_aiohttp_transfer_failure_recovers_with_identity_request(reader, monkeypatch, first):
    """Real HTTP framing/decompression; only the remote URL is redirected to loopback."""
    files, connectors = reader
    requests, options = [], []

    async def serve(request):
        requests.append(request.headers['Accept-Encoding'])
        if len(requests) == 1:
            if first == 'encoded':
                return web.Response(body=gzip.compress(b'x'*(2*1024*1024)), headers={'Content-Encoding': 'gzip'})
            response = web.StreamResponse(headers={'Content-Length': str(len(CONTENT))})
            await response.prepare(request)
            await response.write(CONTENT[:5])
            request.transport.close()
            return response
        return web.Response(body=CONTENT)

    app = web.Application()
    app.router.add_get('/file', serve)
    actual_session = aiohttp.ClientSession
    async with TestServer(app) as server:
        class LocalSession:
            def __init__(self, **kwargs):
                options.append(kwargs)
                self.client = actual_session(**kwargs)
            async def __aenter__(self):
                await self.client.__aenter__()
                return self
            async def __aexit__(self, *args):
                await self.client.__aexit__(*args)
            def get(self, url, **kwargs):
                assert url == 'https://files.slack.com/files-pri/private'
                return self.client.get(server.make_url('/file'), **kwargs)
        monkeypatch.setattr(slack_files.aiohttp, 'ClientSession', LocalSession)
        _, raw, _ = await files.read(FILE['id'], TEAM)
        try:
            assert raw.read() == CONTENT
        finally:
            raw.close()
    assert requests == ['identity', 'identity']
    assert all(option['auto_decompress'] is False for option in options)
    assert connectors.request.await_count == 2


def test_failed_attachment_records_safe_attempt_history_on_existing_turn(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    monkeypatch.setattr(slack_files, 'RETRY_DELAY_SECONDS', .001)
    provider(app, monkeypatch, raw=b'private file contents', metadata={'size': 1})
    client.post('/hooks/slack/events', **signed(event(files=[FILE])))
    run_id = runs[0]['id']
    message = app.state.store.claim_message(run_id)
    client.portal.call(app.state.slack.files.prepare, run_id)
    row = app.state.store.messages(run_id)[0]
    assert not row['attachments'] and 'Do not guess the missing content' in row['content']
    failures = [entry for entry in app.state.store.events(run_id)
                if entry['data'].get('phase') == 'slack_attachment_read']
    assert len(failures) == 1
    detail = failures[0]['data']
    assert detail['turn_id'] == message['id'] and detail['attempts'] == 2
    assert detail['code'] == 'metadata_size_mismatch' and detail['declared_bytes'] == 1
    assert detail['content_encoding'] == 'identity' and detail['content_encoding_present'] is False
    assert 'received_bytes' not in detail  # Content-Length disagreed before consuming the body.
    assert len(detail['attempt_history']) == 2
    for private in ('bot-token', 'private file contents', 'https://', FILE['id']):
        assert private not in str(failures) + row['content']


@pytest.fixture
def failed_then_successful_batch(slack_app, monkeypatch):
    app, client, runs, _ = slack_app
    monkeypatch.setattr(slack_files, 'RETRY_DELAY_SECONDS', .001)
    calls, _ = provider(app, monkeypatch)
    original_request = app.state.connectors.request

    async def request(method, url, **kwargs):
        result = await original_request(method, url, **kwargs)
        if url.endswith('files.info') and kwargs['params']['file'] == FILE['id']:
            result['file']['size'] = 1
        return result

    monkeypatch.setattr(app.state.connectors, 'request', request)
    other = {**FILE, 'id': 'F87654321'}
    client.post('/hooks/slack/events', **signed(event(files=[FILE, other])))
    run_id = runs[0]['id']
    message = app.state.store.claim_message(run_id)
    return app, client, run_id, message, calls, other


@pytest.mark.parametrize('after_record', [False, True])
def test_optional_diagnostic_failure_preserves_explanation_and_successful_batch(
        failed_then_successful_batch, monkeypatch, after_record):
    app, client, run_id, message, calls, other = failed_then_successful_batch
    store = app.state.store
    record = store.event
    failed_records = []

    def unavailable(identity, kind, text, data=None, **kwargs):
        if (data or {}).get('phase') == 'slack_attachment_read':
            failed_records.append(data)
            if after_record:
                record(identity, kind, text, data, **kwargs)
            raise RuntimeError(PRIVATE)
        return record(identity, kind, text, data, **kwargs)

    monkeypatch.setattr(store, 'event', unavailable)
    checkpoint = AsyncMock()
    monkeypatch.setattr(app.state.slack, 'checkpoints', SimpleNamespace(flush=checkpoint))
    client.portal.call(app.state.slack.files.prepare, run_id)
    row = store.messages(run_id)[0]
    assert calls == [FILE['id'], FILE['id'], other['id']]
    assert len(row['attachments']) == 1 and row['attachments'][0]['size'] > 1
    assert 'metadata_size_mismatch' in row['content'] and 'Do not guess' in row['content']
    assert PRIVATE not in row['content']
    assert store.rows('SELECT status FROM slack_audio_inputs WHERE message_id=?', (message['id'],))[0]['status'] == 'ready'
    assert len(failed_records) == 1 and failed_records[0]['code'] == 'metadata_size_mismatch'
    assert checkpoint.await_count >= 1


def test_diagnostic_cancellation_still_stops_preparation(failed_then_successful_batch, monkeypatch):
    app, client, run_id, message, calls, _ = failed_then_successful_batch
    store = app.state.store
    record = store.event

    def cancelled(identity, kind, text, data=None, **kwargs):
        if (data or {}).get('phase') == 'slack_attachment_read':
            raise asyncio.CancelledError()
        return record(identity, kind, text, data, **kwargs)

    monkeypatch.setattr(store, 'event', cancelled)
    async def prepare():
        with pytest.raises(asyncio.CancelledError):
            await app.state.slack.files.prepare(run_id)
    client.portal.call(prepare)
    assert calls == [FILE['id'], FILE['id']]
    assert store.rows('SELECT status FROM slack_audio_inputs WHERE message_id=?', (message['id'],))[0]['status'] == 'pending'
    assert not store.messages(run_id)[0]['attachments']


@pytest.mark.parametrize('failure,code,status', [
    ('http401', 'authentication', 401),
    ('invalid_auth', 'authentication', None),
    ('http503', 'credential_upstream', 503),
    ('connect', 'credential_transport', None),
    ('response_lost', 'credential_unconfirmed', None),
    ('invalid_json', 'credential_unconfirmed', None),
    ('http429', 'rate_limited', 429),
    ('http400', 'credential_unavailable', 400),
])
def test_real_oauth_refresh_failure_is_not_a_file_get_retry(
        slack_app, monkeypatch, failure, code, status):
    """Actual token acquisition -> exchange -> request; only HTTPS transport is controlled."""
    app, client, *_ = slack_app
    connectors = app.state.connectors
    app.state.settings.slack_client_id = 'fixture-client'
    app.state.settings.slack_client_secret = 'fixture-client-secret'
    credentials = {'access_token': 'unused-user-secret', 'kind': 'oauth', 'bot': {
        'access_token': 'expired-bot-secret', 'refresh_token': 'fixture-refresh-secret', 'expires_at': 1,
        'team': {'id': TEAM}, 'bot_user_id': 'U99999999', 'scope': 'files:read'}}
    connectors.save('slack', credentials, 'Fixture team')
    saved = app.state.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]['encrypted']
    monkeypatch.setattr(connectors, 'request', Connectors.request.__get__(connectors))
    attempts = []

    def upstream(request):
        attempts.append((request.method, request.url.path))
        assert request.method == 'POST' and request.url.path == '/api/oauth.v2.access'
        assert parse_qs(request.content.decode()) == {
            'grant_type': ['refresh_token'], 'refresh_token': ['fixture-refresh-secret'],
            'client_id': ['fixture-client'], 'client_secret': ['fixture-client-secret']}
        if failure == 'connect':
            raise httpx.ConnectError(PRIVATE, request=request)
        if failure == 'response_lost':
            raise httpx.ReadError(PRIVATE, request=request)
        if failure == 'invalid_json':
            return httpx.Response(200, text=PRIVATE)
        if failure == 'invalid_auth':
            return httpx.Response(200, json={'ok': False, 'error': 'invalid_auth', 'detail': PRIVATE})
        return httpx.Response(status, headers={'Retry-After': '0.001'} if status == 429 else {},
                              json={'error': PRIVATE})

    actual = httpx.AsyncClient
    class ProviderClient(actual):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(upstream), **kwargs)
    monkeypatch.setattr('app.connectors.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))
    with pytest.raises(SlackFileError) as caught:
        client.portal.call(app.state.slack.files.read, FILE['id'], TEAM)
    detail = caught.value.diagnostic()
    assert detail['code'] == code and detail['stage'] == 'access'
    assert detail['http_status'] == status and detail['attempts'] == 1
    assert not caught.value.retryable
    assert attempts == [('POST', '/api/oauth.v2.access')]
    assert app.state.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]['encrypted'] == saved
    assert 'received_bytes' not in detail and 'content_encoding' not in detail
    if code != 'authentication':
        assert 'access was denied' not in str(caught.value) and 'Reconnect Slack' not in detail['next_action']
        assert 'OAuth refresh POST' in detail['remaining_blocker']
    for private in (PRIVATE, 'expired-bot-secret', 'fixture-client-secret', 'fixture-refresh-secret', 'unused-user-secret'):
        assert private not in str(caught.value) + json.dumps(detail)


def test_real_oauth_refresh_success_can_continue_to_file_read(slack_app, monkeypatch):
    app, client, *_ = slack_app
    connectors = app.state.connectors
    app.state.settings.slack_client_id = 'fixture-client'
    app.state.settings.slack_client_secret = 'fixture-client-secret'
    connectors.save('slack', {'access_token': 'unused-user-secret', 'bot': {
        'access_token': 'expired-bot-secret', 'refresh_token': 'fixture-refresh-secret', 'expires_at': 1,
        'team': {'id': TEAM}, 'bot_user_id': 'U99999999', 'scope': 'files:read'}}, 'Fixture team')
    monkeypatch.setattr(connectors, 'request', Connectors.request.__get__(connectors))
    attempts = []

    def upstream(request):
        attempts.append((request.method, request.url.path))
        if request.url.path == '/api/oauth.v2.access':
            return httpx.Response(200, json={'ok': True, 'access_token': 'xoxb-secret',
                                            'refresh_token': 'rotated-refresh-secret', 'expires_in': 3600})
        assert request.url.path == '/api/files.info' and request.headers['Authorization'] == 'Bearer xoxb-secret'
        return httpx.Response(200, json=metadata())

    actual = httpx.AsyncClient
    class ProviderClient(actual):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(upstream), **kwargs)
    monkeypatch.setattr('app.connectors.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))
    calls, _ = downloads(monkeypatch, {})
    _, raw, _ = client.portal.call(app.state.slack.files.read, FILE['id'], TEAM)
    try:
        assert raw.read() == CONTENT
    finally:
        raw.close()
    assert attempts == [('POST', '/api/oauth.v2.access'), ('GET', '/api/files.info')] and len(calls) == 1
    saved = json.loads(app.state.security.decrypt(
        app.state.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]['encrypted']))
    assert saved['bot']['access_token'] == 'xoxb-secret' and saved['access_token'] == 'unused-user-secret'
