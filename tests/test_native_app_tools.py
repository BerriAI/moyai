"""Broker contract tests: finite operations, actual payloads and failure boundaries."""
import copy
import json

import httpx
import pytest

from app.connectors import TOOLS
from test_connector_coverage import invoke, transport
from test_slack_sender import CREDENTIALS
from test_workspace import cloud_capability, workspace

ID = '12345678-1234-1234-1234-123456789abc'
OTHER = 'abcdefab-1234-1234-1234-123456789abc'
TS = '1790719999.123456'
CHANNEL = 'C12345678'


@pytest.fixture
def native(workspace, monkeypatch):
    app, client = workspace
    run, headers = cloud_capability(app, ['linear', 'notion', 'slack'])
    creds = copy.deepcopy(CREDENTIALS)
    creds['bot']['scope'] += ',reactions:read,reactions:write'
    app.state.connectors.save('slack', creds, 'Test bot')
    app.state.store.identity({'method': 'google', 'identity': {'sub': 'native', 'email': 'native@berri.ai', 'name': 'Native Tester'}})
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:native', run))
    calls = []
    responses = []

    def upstream(request):
        calls.append(request)
        assert request.url.host in {'api.linear.app', 'api.notion.com', 'slack.com'}
        assert responses, f"Unexpected request {request.method} {request.url.path}"
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(request)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    transport(monkeypatch, upstream)
    def call(name, arguments):
        return invoke(client, run, headers, name, arguments)
    return app, client, run, headers, calls, responses, call


def issue(**fields):
    return {'id': ID, 'title': 'old', 'parent': {'id': OTHER}, **fields}


def message(**fields):
    return {'ok': True, 'type': 'message', 'channel': CHANNEL,
            'message': {'ts': TS, 'user': CREDENTIALS['bot']['bot_user_id'], **fields}}


def test_linear_patch_preserves_omission_through_both_broker_serializations(native):
    *_, calls, responses, call = native
    responses.extend([{'data': {'issue': issue()}}, {'data': {'issueUpdate': {'success': True, 'issue': issue(title='new')}}}])
    result = call('linear_update_issue', {'issue_id': 'TEST-1', 'title': 'new'})
    assert result['issueUpdate']['success']
    assert json.loads(calls[-1].content)['variables']['input'] == {'title': 'new'}


def test_linear_all_fields_clear_semantics_and_readback(native):
    *_, calls, responses, call = native
    patch = dict(title='fixed', description=None, state_id=OTHER, assignee_id=None, priority=0,
                 label_ids=[], project_id=None, cycle_id=None, due_date=None, parent_id=None)
    updated = issue(title='fixed', description='', state={'id': OTHER}, assignee=None, priority=0,
                    labels={'nodes': [], 'pageInfo': {'hasNextPage': False}},
                    project=None, cycle=None, dueDate=None, parent=None)
    responses.extend([{'data': {'issue': issue()}}, {'data': {'issueUpdate': {'success': True, 'issue': updated}}}])
    assert call('linear_update_issue', {'issue_id': 'TEST-1', **patch})['issueUpdate']['success']
    assert json.loads(calls[-1].content)['variables']['input'] == {
        'title': 'fixed', 'description': None, 'stateId': OTHER, 'assigneeId': None, 'priority': 0,
        'labelIds': [], 'projectId': None, 'cycleId': None, 'dueDate': None, 'parentId': None}


def test_linear_relationship_values_and_label_set_are_confirmed(native):
    *_, calls, responses, call = native
    updated = issue(state={'id': OTHER}, assignee={'id': OTHER}, project={'id': OTHER}, cycle={'id': OTHER},
                    labels={'nodes': [{'id': OTHER}], 'pageInfo': {'hasNextPage': False}}, dueDate='2026-10-15')
    responses.extend([{'data': {'issue': issue()}}, {'data': {'issueUpdate': {'success': True, 'issue': updated}}}])
    result = call('linear_update_issue', {'issue_id': ID, 'state_id': OTHER.upper(), 'assignee_id': OTHER,
        'project_id': OTHER, 'cycle_id': OTHER, 'label_ids': [OTHER], 'due_date': '2026-10-15'})
    assert result['issueUpdate']['success']
    assert 'parentId' not in json.loads(calls[-1].content)['variables']['input']


def test_linear_issue_labels_have_independent_usable_pagination(native):
    *_, calls, responses, call = native
    for more in [True, False]:
        responses.append({'data': {'issue': issue(
            comments={'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'comments2'}},
            labels={'nodes': [{'id': OTHER}], 'pageInfo': {'hasNextPage': more, 'endCursor': 'labels2'}})}})
    result = call('linear_issue', {'issue_id': ID, 'cursor': 'comments1', 'page_size': 1})
    assert result['next_arguments']['cursor'] == 'comments2'
    result = call('linear_issue', result['labels_next_arguments'])
    variables = json.loads(calls[-1].content)['variables']
    assert variables['after'] == 'comments1' and variables['labelAfter'] == 'labels2'
    assert result['labels_next_arguments'] is None and result['labels_coverage']['has_more'] is False


@pytest.mark.parametrize('resource', ['states', 'members', 'labels', 'projects', 'cycles'])
def test_linear_discovery_paginates_team_collection(native, resource):
    *_, calls, responses, call = native
    responses.extend([{'data': {'team': {'id': ID, resource: {
        'nodes': [{'id': OTHER}], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}}}}},
        {'data': {'team': {'id': ID, resource: {'nodes': [], 'pageInfo': {'hasNextPage': False}}}}}])
    result = call('linear_options', {'team_id': ID, 'resource': resource})
    result = call('linear_options', result['next_arguments'])
    assert result['coverage']['has_more'] is False
    assert json.loads(calls[-1].content)['variables']['after'] == 'next'
    assert resource + '(first:$first,after:$after)' in json.loads(calls[-1].content)['query']


@pytest.mark.parametrize('patch', [
    {}, {'title': None}, {'state_id': None}, {'label_ids': None}, {'priority': None},
    {'priority': True}, {'priority': 5}, {'due_date': '2026-02-30'}, {'due_date': '20261013'},
    {'state_id': 'Done'}, {'url': 'https://evil.example'}, {'label_ids': ['bad']},
])
def test_invalid_linear_patch_stops_before_provider(native, patch):
    *_, calls, responses, call = native
    result = call('linear_update_issue', {'issue_id': ID, **patch})
    assert result['code'] == 'invalid_arguments' and not calls


def test_linear_failed_preflight_is_not_an_unknown_write(native):
    *_, calls, responses, call = native
    responses.append(httpx.ReadTimeout('secret detail'))
    result = call('linear_update_issue', {'issue_id': ID, 'title': 'new'})
    assert result['dispatched'] is False and not result['outcome_uncertain']
    assert len(calls) == 1 and 'secret' not in json.dumps(result)


@pytest.mark.parametrize('field,value,actual', [('title', 'new', 'old'), ('due_date', '2026-10-15', None)])
def test_linear_unconfirmed_field_is_unknown_never_replayed(native, field, value, actual):
    *_, calls, responses, call = native
    responses.extend([{'data': {'issue': issue()}}, {'data': {'issueUpdate': {
        'success': True, 'issue': issue(**{('dueDate' if field == 'due_date' else field): actual})}}}])
    result = call('linear_update_issue', {'issue_id': ID, field: value})
    assert result['outcome_uncertain'] and not result['retryable'] and len(calls) == 2


def test_notion_database_data_source_discovery_query_and_property_pages(native):
    *_, calls, responses, call = native
    responses.extend([
        {'object': 'database', 'id': ID, 'data_sources': [{'id': OTHER, 'name': 'Tasks'}]},
        {'object': 'data_source', 'id': OTHER, 'properties': {'Done': {'type': 'checkbox'}}},
        {'object': 'list', 'results': [], 'has_more': True, 'next_cursor': 'query2'},
        {'object': 'list', 'results': [{'id': ID}], 'has_more': False},
        {'object': 'list', 'results': [], 'has_more': True, 'next_cursor': 'prop2'},
        {'object': 'list', 'results': [], 'has_more': False},
    ])
    assert call('notion_object', {'object_id': ID, 'kind': 'database'})['data_sources'][0]['id'] == OTHER
    call('notion_object', {'object_id': OTHER, 'kind': 'data_source'})
    result = call('notion_query', {'data_source_id': OTHER,
        'filters': [{'property': 'Done', 'type': 'checkbox', 'operator': 'equals', 'value': False}],
        'sorts': [{'property': 'Name', 'direction': 'ascending'}]})
    result = call('notion_query', result['next_arguments'])
    assert result['next_arguments'] is None
    result = call('notion_property', {'page_id': ID, 'property_id': 'A%3AB'})
    call('notion_property', result['next_arguments'])
    assert all(r.headers['Notion-Version'] == '2025-09-03' for r in calls)
    assert calls[2].url.path == f'/v1/data_sources/{OTHER}/query'
    assert json.loads(calls[2].content)['filter'] == {'and': [{'property': 'Done', 'checkbox': {'equals': False}}]}
    assert json.loads(calls[3].content)['start_cursor'] == 'query2'
    assert calls[-1].url.params['start_cursor'] == 'prop2'
    assert calls[-1].url.raw_path.startswith(f'/v1/pages/{ID}/properties/A%3AB'.encode())


def test_notion_create_update_blocks_and_archive_use_finite_payloads(native):
    *_, calls, responses, call = native
    block = {'type': 'to_do', 'rich_text': [{'text': 'Ship', 'bold': True}], 'checked': False}
    responses.extend([
        {'object': 'page', 'id': ID},
        {'object': 'page', 'id': ID},
        {'object': 'list', 'results': [{'id': OTHER}]},
        {'object': 'block', 'id': OTHER, 'type': 'to_do'},
        {'object': 'block', 'id': OTHER},
        {'object': 'block', 'id': OTHER, 'archived': True},
    ])
    assert call('notion_create_page', {'parent_id': OTHER, 'parent_type': 'data_source_id',
        'properties': [{'property': 'Name', 'type': 'title', 'text': 'Release'}], 'children': [block]})['id'] == ID
    call('notion_update_page', {'page_id': ID, 'properties': [
        {'property': 'Due', 'type': 'date', 'value': None}, {'property': 'Owners', 'type': 'people', 'ids': []}]})
    call('notion_append_blocks', {'block_id': ID, 'after': OTHER, 'children': [block]})
    call('notion_update_block', {'block_id': OTHER, 'block': {**block, 'checked': True}})
    call('notion_update_block', {'block_id': OTHER, 'archived': True})
    assert json.loads(calls[0].content)['parent'] == {'data_source_id': OTHER}
    assert json.loads(calls[1].content) == {'properties': {'Due': {'date': None}, 'Owners': {'people': []}}}
    assert json.loads(calls[2].content)['after'] == OTHER
    assert json.loads(calls[4].content)['to_do']['checked'] is True
    assert json.loads(calls[5].content) == {'archived': True}


@pytest.mark.parametrize('arguments', [
    {'page_id': ID}, {'page_id': ID, 'in_trash': None},
    {'page_id': ID, 'properties': [{'property': 'Name', 'type': 'formula', 'value': 'x'}]},
    {'page_id': ID, 'properties': [], 'url': 'https://evil.example'},
])
def test_notion_invalid_patch_never_dispatched(native, arguments):
    *_, calls, responses, call = native
    assert call('notion_update_page', arguments)['code'] == 'invalid_arguments'
    assert not calls


@pytest.mark.parametrize('failure', ['lost', 'wrong_id', 'missing_receipt', 'rate_limit'])
def test_notion_write_receipts_and_unknown_outcomes(native, failure):
    *_, calls, responses, call = native
    responses.append({'lost': httpx.ReadTimeout('provider secret'),
        'wrong_id': {'object': 'page', 'id': OTHER}, 'missing_receipt': {},
        'rate_limit': httpx.Response(429, headers={'retry-after': '7'}, text='provider secret')}[failure])
    result = call('notion_update_page', {'page_id': ID, 'in_trash': True})
    assert len(calls) == 1 and 'provider secret' not in json.dumps(result)
    assert result['outcome_uncertain'] == (failure != 'rate_limit')
    assert result['retryable'] == (failure == 'rate_limit')


def test_notion_block_type_check_prevents_changing_type(native):
    *_, calls, responses, call = native
    responses.append({'object': 'block', 'id': ID, 'type': 'paragraph'})
    result = call('notion_update_block', {'block_id': ID, 'block': {'type': 'to_do'}})
    assert result['code'] == 'block_type_mismatch' and len(calls) == 1


def test_notion_discovery_warns_on_unusable_continuation(native):
    *_, calls, responses, call = native
    responses.append({'object': 'list', 'results': [], 'has_more': True, 'next_cursor': 'same'})
    result = call('notion_discover', {'kind': 'data_source', 'cursor': 'same'})
    assert result['next_arguments'] is None and result['coverage']['warning']
    assert json.loads(calls[0].content)['filter']['value'] == 'data_source'


def test_notion_encoded_property_id_stays_in_single_path_segment(native):
    *_, calls, responses, call = native
    responses.append({'object': 'property_item', 'id': 'id'})
    call('notion_property', {'page_id': ID, 'property_id': '../other?token=anything'})
    assert b'..%2Fother%3Ftoken%3Danything' in calls[0].url.raw_path
    assert 'token' not in calls[0].url.params


def test_notion_payload_limit_rejects_before_network(native):
    *_, calls, responses, call = native
    result = call('notion_create_page', {'parent_type': 'page_id', 'parent_id': ID,
        'properties': [{'property': 'title', 'type': 'title', 'text': '界' * 200000}]})
    assert result['code'] == 'payload_too_large' and not calls


@pytest.mark.parametrize('filters', [
    [{'property': 'Done', 'type': 'checkbox', 'operator': 'equals', 'value': 'false'}],
    [{'property': 'Status', 'type': 'status', 'operator': 'contains', 'value': 'Done'}],
    [{'property': 'Count', 'type': 'number', 'operator': 'equals', 'value': True}],
])
def test_notion_filters_are_typed_and_finite(native, filters):
    *_, calls, responses, call = native
    assert call('notion_query', {'data_source_id': ID, 'filters': filters})['code'] == 'invalid_arguments'
    assert not calls


def test_slack_explicit_reply_continuations_stay_in_original_thread(native):
    *_, calls, responses, call = native
    responses.extend([message(), {'ok': True, 'channel': CHANNEL, 'ts': '1790719999.234567'},
                      {'ok': True, 'channel': CHANNEL, 'ts': '1790719999.345678'}])
    result = call('slack_reply', {'channel': CHANNEL, 'thread_ts': TS, 'text': 'x' * 50000})
    assert result['complete'] and len(result['parts']) == 2
    assert all(json.loads(r.content)['thread_ts'] == TS for r in calls[1:])
    assert all(r.headers['Authorization'] == 'Bearer ' + CREDENTIALS['bot']['access_token'] for r in calls)


def test_slack_partial_reply_preserves_receipt_without_replaying(native):
    *_, calls, responses, call = native
    responses.extend([message(), {'ok': True, 'channel': CHANNEL, 'ts': '1790719999.234567'},
                      httpx.ReadTimeout('lost second part')])
    result = call('slack_reply', {'channel': CHANNEL, 'thread_ts': TS, 'text': 'x' * 50000})
    assert result['outcome_uncertain'] and not result['retryable']
    assert result['acknowledged_messages'] == [{'channel': CHANNEL, 'ts': '1790719999.234567'}]
    assert len(calls) == 3
    assert all(json.loads(r.content)['thread_ts'] == TS for r in calls[1:])


@pytest.mark.parametrize('action,code', [('add', 'already_reacted'), ('remove', 'no_reaction')])
def test_slack_converged_reaction_is_success_not_a_failed_write(native, action, code):
    *_, calls, responses, call = native
    responses.extend([message(), {'ok': False, 'error': code}])
    result = call('slack_reaction', {'channel': CHANNEL, 'ts': TS, 'name': 'eyes', 'action': action})
    assert result['ok'] and result['already_in_state'] and 'error' not in result


@pytest.mark.parametrize('problem', ['no_bot', 'no_scope', 'missing_identity'])
def test_slack_mutation_never_falls_back_to_a_user_token(native, problem):
    app, _, _, _, calls, responses, call = native
    credentials = copy.deepcopy(CREDENTIALS)
    if problem == 'no_bot':
        credentials.pop('bot')
    elif problem == 'missing_identity':
        credentials['bot'].update(scope='chat:write,reactions:read')
        credentials['bot'].pop('bot_user_id')
    app.state.connectors.save('slack', credentials, 'Test')
    result = call('slack_edit_message', {'channel': CHANNEL, 'ts': TS, 'text': 'new'})
    assert result['dispatched'] is False and not calls


@pytest.mark.parametrize('name,args,endpoint', [
    ('slack_edit_message', {'text': 'Corrected'}, 'chat.update'),
    ('slack_delete_message', {}, 'chat.delete'),
    ('slack_reaction', {'name': 'white_check_mark', 'action': 'add'}, 'reactions.add'),
    ('slack_reaction', {'name': 'white_check_mark', 'action': 'remove'}, 'reactions.remove'),
])
def test_slack_mutations_are_bot_only_and_target_exact_message(native, name, args, endpoint):
    *_, calls, responses, call = native
    responses.extend([message(), {'ok': True, 'channel': CHANNEL, 'ts': TS}])
    assert call(name, {'channel': CHANNEL, 'ts': TS, **args})['ok']
    assert calls[-1].url.path == '/api/' + endpoint
    assert all(r.headers['Authorization'] == 'Bearer ' + CREDENTIALS['bot']['access_token'] for r in calls)
    if name == 'slack_edit_message':
        assert json.loads(calls[-1].content)['text'] == 'Native Tester: Corrected'


@pytest.mark.parametrize('name', ['slack_edit_message', 'slack_delete_message'])
def test_slack_foreign_author_cannot_be_mutated(native, name):
    *_, calls, responses, call = native
    responses.append(message(user='U22222222'))
    result = call(name, {'channel': CHANNEL, 'ts': TS, **({'text': 'overwrite'} if name == 'slack_edit_message' else {})})
    assert result['code'] == 'not_bot_message' and len(calls) == 1


@pytest.mark.parametrize('mismatch', ['channel', 'timestamp', 'reply'])
def test_slack_reply_verifies_channel_and_root(native, mismatch):
    *_, calls, responses, call = native
    response = message(**({'ts': '1790719999.111111'} if mismatch == 'timestamp' else
                         {'thread_ts': '1790719999.111111'} if mismatch == 'reply' else {}))
    if mismatch == 'channel':
        response['channel'] = 'C99999999'
    responses.append(response)
    result = call('slack_reply', {'channel': CHANNEL, 'thread_ts': TS, 'text': 'Reply'})
    assert result['dispatched'] is False and len(calls) == 1


def test_slack_read_loss_is_unsent_but_mutation_loss_is_unknown(native):
    *_, calls, responses, call = native
    args = {'channel': CHANNEL, 'ts': TS, 'text': 'update'}
    responses.append(httpx.ReadTimeout('secret'))
    assert not call('slack_edit_message', args)['outcome_uncertain']
    responses.extend([message(), httpx.ReadTimeout('secret')])
    result = call('slack_edit_message', args)
    assert result['outcome_uncertain'] and not result['retryable'] and len(calls) == 3


NEW_WRITES = [
    ('linear_update_issue', {'issue_id': ID, 'title': 'Changed'}),
    ('notion_create_page', {'parent_type': 'page_id', 'parent_id': ID,
        'properties': [{'property': 'title', 'type': 'title', 'text': 'Page'}]}),
    ('notion_update_page', {'page_id': ID, 'in_trash': True}),
    ('notion_append_blocks', {'block_id': ID, 'children': [{'type': 'paragraph'}]}),
    ('notion_update_block', {'block_id': ID, 'archived': True}),
    ('slack_reply', {'channel': CHANNEL, 'thread_ts': TS, 'text': 'Reply'}),
    ('slack_edit_message', {'channel': CHANNEL, 'ts': TS, 'text': 'Edited'}),
    ('slack_delete_message', {'channel': CHANNEL, 'ts': TS}),
    ('slack_reaction', {'channel': CHANNEL, 'ts': TS, 'name': 'eyes', 'action': 'add'}),
]


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnected', 'unselected', 'cancelled'])
@pytest.mark.parametrize('name,args', NEW_WRITES)
def test_all_new_writes_respect_broker_and_policy_gates(native, restriction, name, args):
    app, client, run, headers, calls, responses, call = native
    provider = TOOLS[name][0]
    if restriction in {'read_only', 'disabled'}:
        app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                (provider, restriction != 'disabled', restriction == 'read_only'))
    elif restriction == 'disconnected':
        app.state.store.execute('DELETE FROM connections WHERE provider=?', (provider,))
    elif restriction == 'unselected':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run,))
    else:
        app.state.store.update_run(run, status='cancelled')
    result = client.post(f'/broker/{run}/tools/call', headers=headers, json={'name': name, 'arguments': args})
    assert result.status_code in {200, 401, 403}
    if result.status_code == 200:
        assert result.json()['dispatched'] is False
    assert not calls


@pytest.mark.parametrize('change', ['read_only', 'unselected', 'requester', 'connection'])
@pytest.mark.parametrize('name,args,preflight', [
    ('linear_update_issue', {'issue_id': ID, 'title': 'new'}, {'data': {'issue': issue()}}),
    ('slack_edit_message', {'channel': CHANNEL, 'ts': TS, 'text': 'new'}, message()),
    ('notion_update_block', {'block_id': ID, 'block': {'type': 'paragraph'}}, {'object': 'block', 'id': ID, 'type': 'paragraph'}),
])
def test_preflight_await_cannot_cross_selection_or_identity_change(native, change, name, args, preflight):
    app, _, run, _, calls, responses, call = native
    provider = TOOLS[name][0]
    def swap(request):
        if change == 'read_only':
            app.state.store.execute('INSERT INTO connection_policies(provider,read_only) VALUES(?,1)', (provider,))
        elif change == 'unselected':
            app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run,))
        elif change == 'requester':
            app.state.store.execute("UPDATE runs SET active_user_id='different-requester' WHERE id=?", (run,))
        else:
            app.state.connectors.save(provider, {'access_token': 'different-connection'}, 'Changed')
        return preflight
    responses.append(swap)
    result = call(name, args)
    assert result['dispatched'] is False and not result['outcome_uncertain'] and len(calls) == 1


@pytest.mark.parametrize('name,args,response', [
    ('slack_message', {'channel': CHANNEL, 'ts': TS}, message()),
    ('notion_object', {'kind': 'page', 'object_id': ID}, {'object': 'page', 'id': ID}),
    ('linear_options', {'team_id': ID, 'resource': 'states'}, {'data': {'team': {'id': ID, 'states': {}}}}),
])
def test_new_reads_do_not_release_data_after_selection_revocation(native, name, args, response):
    app, _, run, _, calls, responses, call = native
    def revoke(request):
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run,))
        return response
    responses.append(revoke)
    result = call(name, args)
    assert result['code'] == 'access_changed' and len(calls) == 1
    assert 'message' not in result and 'team' not in result and 'properties' not in result
