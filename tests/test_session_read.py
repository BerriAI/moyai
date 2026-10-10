import pytest
from fastapi import Request

from app.db import now
from test_spend import active
from test_user_roles import sign_as, users_app


def read(client, caller, **arguments):
    return client.post('/broker/' + caller['id'] + '/tools/call',
                       headers={'Authorization': 'Bearer capability'},
                       json={'name': 'sessions_read', 'arguments': arguments})


def setup(users_app):
    app, client = users_app
    actor = sign_as(app, client, 'maya@berri.ai')['user_id']
    caller = active(app, actor)
    target = app.state.store.create_run('Shared investigation', '', 'demo', [],
                                       chat_enabled=True, user_id='google:other')
    return app, client, actor, caller, target


def test_read_shared_archived_history_and_paginate_without_mutation(users_app):
    app, client, actor, caller, target = setup(users_app)
    store = app.state.store
    store.enqueue_message(target['id'], 'Follow-up evidence', 'second', user_id=actor)
    app.state.session_lifecycle.archive(target['id'], actor, True)
    after_event = store.events(target['id'])[-1]['id']
    store.event(target['id'], 'tool', 'Read result', {'tool': 'github_repository', 'output': {'name': 'moyai'}})
    store.event(target['id'], 'status', 'Waiting for agents', {'phase': 'waiting_children'})
    child = store.create_run('Worker', '', 'demo', [])
    store.execute('UPDATE runs SET parent_run_id=?,agent_label=? WHERE id=?', (target['id'], 'Worker 1', child['id']))
    store.update_run(target['id'], status='waiting_children')
    before = store.run(target['id'])
    url = app.state.settings.public_url + '/#run=' + target['id']
    result = read(client, caller, session=url, limit=1, after_event=after_event)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['session']['status'] == 'waiting_children'
    assert body['children'][0]['id'] == child['id']
    assert body['messages'][0]['content'] == 'Shared investigation'
    assert 'moyai' in body['events'][0]['data']['output']
    assert body['next_message'] and body['next_event']
    second = read(client, caller, session=target['id'], limit=1,
                  after_message=body['next_message'], after_event=body['next_event']).json()
    assert second['messages'][0]['content'] == 'Follow-up evidence'
    assert second['events'][0]['message'] == 'Waiting for agents'
    assert second['next_message'] is None and second['next_event'] is None
    assert store.run(target['id']) == before
    assert target['id'] in app.state.session_lifecycle.archives(actor)
    assert read(client, caller, session=target['id'], after_message=999999, after_event=999999).json()['messages'] == []


def test_private_payloads_reasoning_and_credentials_are_omitted(users_app):
    app, client, actor, caller, target = setup(users_app)
    store = app.state.store
    for tool in ('memory_search', 'mcp__moyai__skills_load', 'credentials_request'):
        store.event(target['id'], 'tool', 'Private operation',
                    {'tool': tool, 'input': 'private library body', 'output': 'private library body'})
    store.event(target['id'], 'tool', 'Repository read', {'tool': 'github_repository',
                'output': {'token': 'sensitivevalue', 'name': 'moyai'}, 'raw_trace': 'private library body'})
    store.event(target['id'], 'reasoning', 'hidden thinking')
    store.enqueue_message(target['id'], '<think>hidden thinking</think>token=sensitivevalue ' + 'a' * 9000,
                          'secret-test', user_id=actor)
    result = read(client, caller, session=target['id'])
    assert result.status_code == 200
    assert 'private library body' not in result.text
    assert 'hidden thinking' not in result.text
    assert 'sensitivevalue' not in result.text
    assert result.json()['messages'][-1]['truncated']
    assert all('input' not in event['data'] for event in result.json()['events'][:3])


@pytest.mark.parametrize('args', [{}, {'session': 'x'}, {'session': 'http://['},
    {'session': 'https://wrong.example/#run=' + 'a' * 32}, {'session': 'a'*32, 'limit': 21},
    {'session': 'a'*32, 'after_event': -1}, {'session': 'a'*32, 'owner_id': 'other'}])
def test_invalid_input(users_app, args):
    app, client, actor, caller, target = setup(users_app)
    assert read(client, caller, **args).status_code == 422


@pytest.mark.parametrize('field', ['deleted_at', 'deletion_requested_at'])
def test_deleted_or_deleting_target_is_unavailable(users_app, field):
    app, client, actor, caller, target = setup(users_app)
    app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (now(), target['id']))
    assert read(client, caller, session=target['id']).status_code == 404
    assert read(client, caller, session='a'*32).status_code == 404


@pytest.mark.parametrize('change', ['child', 'automated', 'anonymous', 'no_turn', 'legacy', 'inactive', 'deleted', 'token'])
def test_only_live_direct_chat_can_discover_and_call(users_app, change):
    app, client, actor, caller, target = setup(users_app)
    store, url = app.state.store, '/broker/' + caller['id'] + '/tools'
    headers = {'Authorization': 'Bearer capability'}
    tool = next(t for t in client.get(url, headers=headers).json() if t['name'] == 'sessions_read')
    assert tool['annotations']['readOnlyHint'] is True
    if change == 'automated':
        store.execute("INSERT INTO automations(id,owner_id,definition,created_at,updated_at) VALUES('read-auto',?,'{}',?,?)",
                      (actor, now(), now()))
        store.execute("INSERT INTO automation_runs VALUES('read-tick','read-auto',1,?,'started','',?)", (caller['id'], now()))
    else:
        field, value = {'child': ('parent_run_id', target['id']), 'anonymous': ('active_user_id', ''),
                        'no_turn': ('active_message_id', None), 'legacy': ('chat_enabled', 0), 'inactive': ('status', 'idle'),
                        'deleted': ('deleted_at', now()), 'token': ('token_hash', '')}[change]
        store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, caller['id']))
    listing = client.get(url, headers=headers)
    assert listing.status_code == 401 or 'sessions_read' not in {t['name'] for t in listing.json()}
    assert read(client, caller, session=target['id']).status_code in {401, 403}


@pytest.mark.parametrize('field,value', [('active_user_id', 'google:other'), ('active_message_id', 999999)])
def test_requester_rechecked_after_body_read(users_app, monkeypatch, field, value):
    app, client, actor, caller, target = setup(users_app)
    original = Request.body

    async def changed_body(request):
        body = await original(request)
        if request.url.path == '/broker/' + caller['id'] + '/tools/call':
            app.state.store.execute(f'UPDATE runs SET {field}=? WHERE id=?', (value, caller['id']))
        return body

    monkeypatch.setattr(Request, 'body', changed_body)
    assert read(client, caller, session=target['id']).status_code == 409
