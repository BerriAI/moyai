"""Research reaches later pages/nested evidence, with recoverable error outcomes."""
import json

import httpx
import pytest

from test_workspace import cloud_capability, workspace
from test_slack_sender import sender, invoke as send


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient',
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def invoke(client, run, headers, name, arguments):
    response = client.post(f'/broker/{run}/tools/call', headers=headers,
                           json={'name': name, 'arguments': arguments})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('name', ['slack_thread', 'slack_channel', 'slack_search'])
def test_slack_retrieves_later_evidence_without_changing_query(workspace, monkeypatch, name):
    app, client = workspace
    run, headers = cloud_capability(app, ['slack'])
    calls = []
    def upstream(request):
        args = dict(request.url.params)
        calls.append(args)
        page = int(args.get('page', args.get('cursor', '1')))
        messages = [{'ts': str(i), 'text': 'later evidence' if i == 119 else 'source '+str(i)}
                    for i in range((page-1)*40, page*40)]
        if name == 'slack_search':
            assert args['query'] == 'in:<#C12345678> evidence'
            return httpx.Response(200, json={'ok': True, 'messages': {'matches': messages, 'paging': {'page': page, 'pages': 3}}})
        assert args['channel'] == 'C12345678'
        return httpx.Response(200, json={'ok': True, 'messages': messages, 'has_more': page < 3,
            'response_metadata': {'next_cursor': str(page+1) if page < 3 else ''}})
    transport(monkeypatch, upstream)
    args = {'query': 'in:C12345678 evidence'} if name == 'slack_search' else {'channel': 'C12345678'}
    if name == 'slack_thread':
        args['thread_ts'] = '1790719999.123456'
    found = []
    while args is not None:
        result = invoke(client, run, headers, name, args)
        found.extend(result['messages']['matches'] if name == 'slack_search' else result['messages'])
        args = result['next_arguments']
    assert len(found) == len({item['ts'] for item in found}) == 120
    assert found[-1]['text'] == 'later evidence'
    assert result['coverage']['has_more'] is False and not result['coverage']['warning']
    assert len(calls) == 3


def test_notion_search_and_nested_children_keep_full_bodies(workspace, monkeypatch):
    app, client = workspace
    run, headers = cloud_capability(app, ['notion'])
    root, nested, final = 'a'*32, 'b'*32, 'c'*32
    body = 'Nested full evidence. '*2000+'Last condition.'
    def upstream(request):
        if request.url.path.endswith('/search'):
            args = json.loads(request.content)
            second = args.get('start_cursor') == 'page-2'
            return httpx.Response(200, json={'results': [{'id': root}] if second else [],
                'has_more': not second, 'next_cursor': None if second else 'page-2'})
        second = request.url.params.get('start_cursor') == 'block-2'
        if root in request.url.path:
            return httpx.Response(200, json={'results': [{'id': nested, 'has_children': True}] if second else [],
                'has_more': not second, 'next_cursor': None if second else 'block-2'})
        assert nested in request.url.path
        return httpx.Response(200, json={'results': [{'id': final, 'has_children': False, 'paragraph': {'rich_text': [{'plain_text': body}]}}],
                                         'has_more': False, 'next_cursor': None})
    transport(monkeypatch, upstream)
    result = invoke(client, run, headers, 'notion_search', {'query': 'Research'})
    result = invoke(client, run, headers, 'notion_search', result['next_arguments'])
    assert result['results'][0]['id'] == root
    result = invoke(client, run, headers, 'notion_page', {'page_id': root})
    result = invoke(client, run, headers, 'notion_page', result['next_arguments'])
    result = invoke(client, run, headers, 'notion_page', result['child_collections'][0])
    assert result['results'][0]['paragraph']['rich_text'][0]['plain_text'] == body
    assert result['child_collections'] == []


@pytest.mark.parametrize('name', ['linear_search', 'linear_teams', 'linear_issue'])
def test_linear_reaches_last_collection_page(workspace, monkeypatch, name):
    app, client = workspace
    run, headers = cloud_capability(app, ['linear'])
    def upstream(request):
        data = json.loads(request.content)
        variables = data['variables']
        assert variables['first'] == 50
        page = int(variables['after'] or 1)
        collection = {'nodes': [{'id': page, 'body': 'last evidence' if page == 4 else 'prior'}],
                      'pageInfo': {'hasNextPage': page < 4, 'endCursor': str(page+1)}}
        result = {'issue': {'comments': collection}} if name == 'linear_issue' else {
            'issues' if name == 'linear_search' else 'teams': collection}
        return httpx.Response(200, json={'data': result})
    transport(monkeypatch, upstream)
    args = {'query': 'search'} if name == 'linear_search' else {'issue_id': 'TEST-1'} if name == 'linear_issue' else {}
    found = []
    while args is not None:
        result = invoke(client, run, headers, name, args)
        collection = result['issue']['comments'] if name == 'linear_issue' else result['issues' if name == 'linear_search' else 'teams']
        found.extend(collection['nodes'])
        args = result['next_arguments']
    assert len(found) == 4 and found[-1]['body'] == 'last evidence'


def test_incomplete_pagination_and_revoked_connection_do_not_claim_coverage(workspace, monkeypatch):
    app, client = workspace
    run, headers = cloud_capability(app, ['notion'])
    transport(monkeypatch, lambda _: httpx.Response(200, json={'results': [], 'has_more': True, 'next_cursor': 'same'}))
    result = invoke(client, run, headers, 'notion_search', {'query': 'work', 'cursor': 'same'})
    assert result['next_arguments'] is None and result['coverage']['warning']
    app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('notion',0)")
    result = invoke(client, run, headers, 'notion_search', {'query': 'work'})
    assert result['dispatched'] is False and result['outcome'] == 'rejected'


@pytest.mark.parametrize('failure,expected', [
    ('validation', ('rejected', False, False)), ('connect', ('rejected', False, True)),
    ('rate', ('rejected', True, True)), ('lost', ('unknown', True, False)),
    ('decode', ('unknown', True, False)),
])
def test_mutation_errors_report_dispatch_and_repair_without_payload_leaks(workspace, monkeypatch, failure, expected):
    app, client = workspace
    run, headers = cloud_capability(app, ['notion'])
    calls = []
    def upstream(request):
        calls.append(request)
        if failure == 'connect':
            raise httpx.ConnectError('private provider detail', request=request)
        if failure == 'lost':
            raise httpx.ReadTimeout('private provider detail', request=request)
        if failure == 'rate':
            return httpx.Response(429, headers={'retry-after': '12'}, text='private provider detail')
        return httpx.Response(200, text='private provider detail')
    transport(monkeypatch, upstream)
    args = {'page_id': 'invalid' if failure == 'validation' else 'a'*32, 'text': 'private requested text'}
    result = invoke(client, run, headers, 'notion_append', args)
    assert (result['outcome'], result['dispatched'], result['retryable']) == expected
    assert len(calls) == (0 if failure == 'validation' else 1)
    assert 'private' not in json.dumps(result)
    if failure == 'validation':
        assert result['fields'][0]['path'] == ['page_id']
    if failure == 'rate':
        assert result['retry_after_seconds'] == 12


def test_large_notion_text_survives_provider_transport_boundaries(workspace, monkeypatch):
    app, client = workspace
    run, headers = cloud_capability(app, ['notion'])
    text = 'Full explanation 界\n'*20000+'Last condition.'
    posted = []
    def upstream(request):
        assert len(request.content) <= 500000
        data = json.loads(request.content)
        rich_text = data['children'][0]['paragraph']['rich_text']
        assert len(rich_text) <= 100
        assert all(len(item['text']['content']) <= 2000 for item in rich_text)
        posted.extend(item['text']['content'] for item in rich_text)
        return httpx.Response(200, json={'results': [{'id': str(len(posted))}]})
    transport(monkeypatch, upstream)
    result = invoke(client, run, headers, 'notion_append', {'page_id': 'a'*32, 'text': text})
    assert result['complete'] and result['completed_characters'] == len(text)
    assert ''.join(posted) == text and len(result['results']) > 1


def test_large_slack_body_continues_in_same_thread_with_full_receipts(sender):
    text = 'Complete explanation. '*5000+'Last condition.'
    result = send(sender, arguments={'channel': 'C12345678', 'text': text})
    payloads = [json.loads(request.content) for request in sender[-1]]
    assert ''.join(payload['text'] for payload in payloads) == 'Moe: '+text
    assert all(len(payload['text']) <= 40000 for payload in payloads)
    assert all(payload['thread_ts'] == result['ts'] for payload in payloads[1:])
    assert result['complete'] and len(result['parts']) == len(payloads)


def test_partial_notion_append_stops_on_owner_change_and_preserves_receipts(workspace, monkeypatch):
    app, client = workspace
    run, headers = cloud_capability(app, ['notion'])
    calls = []
    def upstream(request):
        calls.append(request)
        app.state.store.update_run(run, status='cancelled')
        return httpx.Response(200, json={'results': [{'id': 'saved-part'}]})
    transport(monkeypatch, upstream)
    result = invoke(client, run, headers, 'notion_append', {'page_id': 'a'*32, 'text': 'x'*210000})
    assert result['code'] == 'execution_changed' and not result['outcome_uncertain']
    assert result['acknowledged_results'] == [{'id': 'saved-part'}]
    assert result['completed_characters'] == 200000 and len(calls) == 1
