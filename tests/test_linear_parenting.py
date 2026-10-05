"""Existing issue IDs survive reparenting through the real broker/approval path."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.connector_errors import ConnectorError
from test_workspace import cloud_capability, wait_for, workspace


PARENT_ID = '12345678-1234-1234-1234-123456789abc'
CHILD_ID = 'abcdefab-1234-1234-1234-123456789abc'
UPDATE = {'issue_id': 'TEST-2', 'parent_id': 'TEST-1'}
CREATE = {'team_id': PARENT_ID, 'title': 'New sub-issue', 'description': 'Requested work.'}


@pytest.fixture
def linear(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['linear'])
    issues = {
        'TEST-1': {'id': PARENT_ID, 'identifier': 'TEST-1', 'title': 'Parent', 'parent': None},
        'TEST-2': {'id': CHILD_ID, 'identifier': 'TEST-2', 'title': 'Existing child', 'parent': None},
    }
    calls = []

    async def request(method, url, **kwargs):
        assert method == 'POST' and url == 'https://api.linear.app/graphql'
        body = kwargs['json']
        calls.append(body)
        query, variables = body['query'], body['variables']
        if 'issueUpdate(' in query:
            assert app.state.store.approvals(run_id)[-1]['status'] == 'executing'
            issue = next(i for i in issues.values() if i['id'] == variables['id'])
            parent = next((i for i in issues.values() if i['id'] == variables['input']['parentId']), None)
            issue['parent'] = {k: parent[k] for k in ('id', 'identifier', 'title')} if parent else None
            return {'data': {'issueUpdate': {'success': True, 'issue': dict(issue)}}}
        if 'issueCreate(' in query:
            return {'data': {'issueCreate': {'success': True, 'issue': {'id': 'new-issue'}}}}
        assert 'parent{id identifier title url}' in query
        issue = next((i for i in issues.values() if variables['id'] in (i['id'], i['identifier'])), None)
        return {'data': {'issue': dict(issue) if issue else None}}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    return app, client, run_id, headers, issues, calls


def update_with_approval(linear, arguments, decision='approve'):
    app, client, run_id, headers, _, _ = linear
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/broker/{run_id}/tools/call', headers=headers,
                             json={'name': 'linear_update_issue', 'arguments': arguments})
        approval = wait_for(lambda: next((a for a in app.state.store.approvals(run_id)
                                         if a['status'] == 'pending'), None))
        assert not future.done()
        assert approval['arguments'] == arguments
        assert client.post(f"/api/approvals/{approval['id']}", json={'decision': decision}).status_code == 200
        response = future.result(timeout=5)
    assert response.status_code == 200
    return response.json()


def test_parent_tools_are_discoverable_with_explicit_clear_semantics(linear):
    app, client, run_id, headers, _, _ = linear
    tools = {t['name']: t for t in client.get(f'/broker/{run_id}/tools', headers=headers).json()}
    update = tools['linear_update_issue']
    assert update['annotations']['readOnlyHint'] is False
    assert set(update['inputSchema']['required']) == {'issue_id', 'parent_id'}
    assert {'type': 'null'} in update['inputSchema']['properties']['parent_id']['anyOf']
    assert 'parent_id' in tools['linear_create_issue']['inputSchema']['properties']
    assert 'parent_id' not in tools['linear_create_issue']['inputSchema']['required']
    assert app.state.connectors.requires_approval('linear_update_issue')


@pytest.mark.parametrize('issue_id,parent_id', [('TEST-2', 'TEST-1'), (CHILD_ID, PARENT_ID)])
def test_reparent_existing_issue_by_identifier_or_uuid(linear, issue_id, parent_id):
    app, client, run_id, headers, issues, calls = linear
    result = update_with_approval(linear, {'issue_id': issue_id, 'parent_id': parent_id})
    updated = result['issueUpdate']['issue']
    assert updated['id'] == CHILD_ID and updated['parent']['id'] == PARENT_ID
    assert calls[-1]['variables'] == {'id': CHILD_ID, 'input': {'parentId': PARENT_ID}}
    assert 'IssueUpdateInput!' in calls[-1]['query']
    assert len(issues) == 2 and not any('issueCreate(' in c['query'] for c in calls)
    read = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                       json={'name': 'linear_issue', 'arguments': {'issue_id': 'TEST-2'}}).json()
    assert read['issue']['parent']['identifier'] == 'TEST-1'
    assert app.state.store.approvals(run_id)[-1]['status'] == 'completed'


def test_five_existing_tickets_keep_their_ids_and_titles(linear):
    _, _, _, _, issues, calls = linear
    for n in range(3, 7):
        issues[f'TEST-{n}'] = {'id': f'abcdefab-1234-1234-1234-{n:012d}',
                              'identifier': f'TEST-{n}', 'title': f'Existing child {n}', 'parent': None}
    before = {key: (issue['id'], issue['title']) for key, issue in issues.items()}
    for key in list(issues)[1:]:
        result = update_with_approval(linear, {'issue_id': key, 'parent_id': 'TEST-1'})
        assert result['issueUpdate']['issue']['parent']['id'] == PARENT_ID
    assert before == {key: (issue['id'], issue['title']) for key, issue in issues.items()}
    assert sum('issueUpdate(' in c['query'] for c in calls) == 5
    assert not any('issueCreate(' in c['query'] for c in calls)


def test_explicit_null_removes_parent(linear):
    _, _, _, _, issues, calls = linear
    issues['TEST-2']['parent'] = {'id': PARENT_ID}
    result = update_with_approval(linear, {'issue_id': 'TEST-2', 'parent_id': None})
    assert result['issueUpdate']['issue']['parent'] is None
    assert calls[-1]['variables']['input'] == {'parentId': None}
    assert len(calls) == 2


@pytest.mark.parametrize('parent_id', ['TEST-1', PARENT_ID, None])
def test_create_accepts_optional_parent_without_changing_direct_creation(linear, parent_id):
    app, client, run_id, headers, _, calls = linear
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_create_issue', 'arguments': {**CREATE, 'parent_id': parent_id}})
    assert response.json()['issueCreate']['success']
    expected = {'teamId': PARENT_ID, 'title': CREATE['title'], 'description': CREATE['description']}
    if parent_id is not None:
        expected['parentId'] = PARENT_ID
    assert calls[-1]['variables']['input'] == expected
    assert not app.state.store.approvals(run_id)


@pytest.mark.parametrize('arguments', [
    {'issue_id': 'TEST-2'}, {**UPDATE, 'parent_id': ''}, {**UPDATE, 'parent_id': 'not-an-id'},
    {**UPDATE, 'parent_id': '-' * 36}, {**UPDATE, 'issue_id': '../../secrets'},
    {**UPDATE, 'title': 'Unrelated edit'},
])
def test_invalid_updates_never_request_approval_or_reach_linear(linear, arguments):
    app, client, run_id, headers, _, calls = linear
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_update_issue', 'arguments': arguments})
    assert response.status_code == 422
    assert not calls and not app.state.store.approvals(run_id)


@pytest.mark.parametrize('arguments', [
    {'issue_id': 'TEST-999', 'parent_id': 'TEST-1'},
    {'issue_id': 'TEST-2', 'parent_id': 'TEST-999'},
    {'issue_id': 'TEST-2', 'parent_id': CHILD_ID},
])
def test_missing_issues_and_self_parenting_never_mutate(linear, arguments):
    result = update_with_approval(linear, arguments)
    assert result['error']
    assert not any('mutation' in c['query'] for c in linear[-1])


def test_missing_parent_does_not_create_standalone_replacement(linear):
    _, client, run_id, headers, _, calls = linear
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_create_issue', 'arguments': {**CREATE, 'parent_id': 'TEST-999'}})
    assert response.json()['error']
    assert not any('mutation' in c['query'] for c in calls)


def test_denied_parent_update_never_reaches_linear(linear):
    result = update_with_approval(linear, UPDATE, decision='deny')
    assert result['error']
    assert not linear[-1]


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_update_respects_connection_and_session_boundaries(linear, restriction):
    app, client, run_id, headers, _, calls = linear
    if restriction in {'read_only', 'disabled'}:
        app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                ('linear', restriction != 'disabled', restriction == 'read_only'))
    elif restriction == 'disconnect':
        app.state.store.execute("DELETE FROM connections WHERE provider='linear'")
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    else:
        app.state.store.update_run(run_id, token_hash='')
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'linear_update_issue', 'arguments': UPDATE})
    assert response.status_code in {401, 403}
    assert not calls and not app.state.store.approvals(run_id)


def test_read_only_enabled_during_resolution_prevents_mutation(linear, monkeypatch):
    app = linear[0]
    request = app.state.connectors.request

    async def change_policy(*args, **kwargs):
        result = await request(*args, **kwargs)
        app.state.store.execute('INSERT OR REPLACE INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                ('linear', True, True))
        return result

    monkeypatch.setattr(app.state.connectors, 'request', change_policy)
    assert update_with_approval(linear, UPDATE)['error']
    assert not any('mutation' in c['query'] for c in linear[-1])


@pytest.mark.parametrize('failure', ['permission', 'lost_response', 'unconfirmed', 'missing_issue', 'wrong_parent', 'missing_parent'])
def test_failed_or_unconfirmed_update_is_uncertain_and_never_retried(linear, monkeypatch, failure):
    app, _, run_id, _, _, calls = linear
    request = app.state.connectors.request

    async def fail_update(*args, **kwargs):
        body = kwargs['json']
        if 'issueUpdate(' not in body['query']:
            return await request(*args, **kwargs)
        calls.append(body)
        if failure in {'permission', 'lost_response'}:
            raise ConnectorError('Linear rejected the operation or its response was lost.')
        issue = {'id': CHILD_ID, 'parent': None}
        if failure == 'missing_parent':
            issue.pop('parent')
        return {'data': {'issueUpdate': {'success': failure != 'unconfirmed',
                                        'issue': None if failure == 'missing_issue' else issue}}}

    monkeypatch.setattr(app.state.connectors, 'request', fail_update)
    result = update_with_approval(linear, UPDATE)
    assert result['error'] and result['outcome_uncertain'] is True
    assert result['instruction'] == 'Verify the destination before retrying a write.'
    assert sum('issueUpdate(' in c['query'] for c in calls) == 1
    assert not any('issueCreate(' in c['query'] for c in calls)
    assert app.state.store.approvals(run_id)[-1]['status'] == 'uncertain'
