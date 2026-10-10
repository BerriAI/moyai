"""Channel-ID regression through the authenticated broker and Slack HTTP adapter."""
import httpx
import pytest

from test_workspace import cloud_capability, workspace


@pytest.mark.parametrize('query,expected', [
    ('in:C0123456789 before:2026-10-11 after:2026-10-08',
     'in:<#C0123456789> before:2026-10-11 after:2026-10-08'),
    ('in:C0123456789', 'in:<#C0123456789>'),
    ('fallback in:#C0123456789 -in:G12345678', 'fallback in:<#C0123456789> -in:<#G12345678>'),
    ('in:C0123456789\tin:G12345678', 'in:<#C0123456789>\tin:<#G12345678>'),
    ('in:<#C0123456789> from:<@U12345678>', 'in:<#C0123456789> from:<@U12345678>'),
    ('in:product-feedback "All OpenAI Models"', 'in:product-feedback "All OpenAI Models"'),
    ('"in:C0123456789" in:C0123456789', '"in:C0123456789" in:<#C0123456789>'),
    ('"quoted \\" in:C0123456789" in:G12345678', '"quoted \\" in:C0123456789" in:<#G12345678>'),
    ('"unfinished in:C0123456789', '"unfinished in:C0123456789'),
    ('within:C0123456789 in:C0123456789-extra', 'within:C0123456789 in:C0123456789-extra'),
    ('in:C123 in:@teammate in:<@U12345678>', 'in:C123 in:@teammate in:<@U12345678>'),
])
def test_slack_search_preserves_scope_and_returns_matches(workspace, monkeypatch, query, expected):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['slack'])
    requests = []
    match = {'text': 'All OpenAI Models fallback', 'channel': {'id': 'C0123456789'}}

    def upstream(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/api/search.messages'
        assert request.headers['Authorization'] == 'Bearer provider-test-token'
        assert dict(request.url.params) == {'query': expected, 'count': '20', 'highlight': 'false'}
        return httpx.Response(200, json={'ok': True, 'query': expected, 'messages': {'matches': [match]}})

    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'slack_search', 'arguments': {'query': query}})
    assert response.status_code == 200
    assert response.json()['messages']['matches'] == [match]
    assert len(requests) == 1  # No metadata lookup or unscoped retry.


@pytest.mark.parametrize('reply', [
    {'ok': True, 'messages': {'total': 0, 'matches': []}},
    {'ok': False, 'error': 'missing_scope'},
])
def test_empty_or_failed_search_never_broadens_channel_scope(workspace, monkeypatch, reply):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['slack'])
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.url.params['query'] == 'in:<#C0123456789> after:2026-10-08'
        return httpx.Response(200, json=reply)

    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'slack_search', 'arguments': {'query': 'in:C0123456789 after:2026-10-08'}})
    assert response.status_code == 200
    if reply['ok']:
        assert response.json() == reply
    else:
        assert 'error' in response.json()
    assert len(requests) == 1
