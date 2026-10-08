import json
from decimal import Decimal

from app.db import now
from test_spend import active, sign_in
from test_user_roles import change, sign_as, users_app
from test_workspace import workspace


def record(app, user, cost, *, run=None, model='own-model', created_at='2026-10-07T12:00:00+00:00', status='completed'):
    run = run or active(app, user, model)
    request_id = app.state.spend.begin({**run, 'active_user_id': user}, model)
    app.state.store.execute('''UPDATE model_requests SET cost=?,status=?,created_at=?,
        prompt_tokens=100,completion_tokens=20,total_tokens=120 WHERE id=?''',
        (cost, status, created_at, request_id))
    return request_id, run


def report(client, **params):
    response = client.get('/api/spend', params={'start': '2026-10-01', 'end': '2026-10-07', **params})
    assert response.status_code == 200
    return response.json()


def test_personal_report_scopes_all_fields_and_ignores_caller_identity(workspace, monkeypatch):
    app, client = workspace
    alice = sign_in(app, client)
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    other_request, shared = record(app, alice, '100', model='other-model', created_at='2026-10-01T00:00:00+00:00')
    own_request, _ = record(app, bob, '0.123456789012345678', run=shared)
    # A different person's private session and unmatched historical usage must not leak.
    _, other_run = record(app, alice, '50', model='private-model')
    app.state.store.execute('UPDATE runs SET prompt=? WHERE id=?', ('Another person’s session', other_run['id']))
    record(app, '', '80', model='unattributed-model')

    def no_infrastructure(*args):
        raise AssertionError('Personal reports must not even load infrastructure billing')

    monkeypatch.setattr(app.state.spend.infrastructure, 'report', no_infrastructure)
    data = report(client, user_id=alice, user=alice, scope='organization', admin='true')
    assert data['scope'] == 'personal'
    assert data['total']['spend'] == '0.123456789012345678'
    assert data['total']['requests'] == data['total']['sessions'] == data['priced_requests'] == 1
    assert data['total']['total_tokens'] == 120
    assert data['tracked_since'] == '2026-10-07T12:00:00+00:00'
    assert [u['id'] for u in data['users']] == [bob]
    assert [u['id'] for u in data['identities']] == [bob]
    assert [r['id'] for r in data['request_details']] == [own_request]
    assert [s['run_id'] for s in data['sessions']] == [shared['id']]
    assert [m['model'] for m in data['models']] == ['own-model']
    for field in ['users', 'sessions', 'models']:
        assert sum(Decimal(row['spend']) for row in data[field]) == Decimal(data['total']['spend'])
    serialized = json.dumps(data)
    for excluded in [alice, 'alice@berri.ai', other_request, other_run['id'], 'Another person', 'other-model',
                     'private-model', 'unattributed-model', 'infrastructure', 'cost_summary']:
        assert excluded not in serialized
    assert client.get('/api/admin/spend').status_code == 403
    assert client.get('/api/admin/identities/status').status_code == 403


def test_personal_report_includes_only_established_slack_links(workspace):
    app, client = workspace
    alice = sign_in(app, client)
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    for slack_id, target in [('slack:linked', bob), ('slack:other', alice), ('slack:unlinked', None)]:
        # Even the same email is insufficient without an established accounting link.
        app.state.store.execute('''INSERT INTO users(id,kind,email,name,linked_user_id,created_at,updated_at)
            VALUES(?,'slack','bob@berri.ai',?,?,?,?)''', (slack_id, slack_id, target, now(), now()))
        record(app, slack_id, '2')
    record(app, bob, '0.5')
    data = report(client)
    assert data['total']['spend'] == '2.5'
    assert data['total']['requests'] == 2
    assert data['daily'][-1]['active_users'] == 1
    assert {r['user_id'] for r in data['request_details']} == {bob}
    assert {u['id'] for u in data['identities']} == {bob, 'slack:linked'}
    assert [u['id'] for u in data['users']] == [bob]
    assert app.state.store.rows("SELECT user_id FROM model_requests WHERE user_id='slack:linked'")
    sign_in(app, client)
    assert client.post('/api/admin/spend/link-slack', json={
        'slack_user_id': 'slack:linked', 'google_user_id': alice}).status_code == 200
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert report(client)['total']['spend'] == '0.5'


def test_personal_dates_empty_history_and_unknown_costs(workspace):
    app, client = workspace
    alice = sign_in(app, client)
    record(app, alice, '100', created_at='2026-09-01T12:00:00+00:00')
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    empty = report(client)
    assert empty['total']['spend'] == '0' and empty['total']['requests'] == 0
    assert empty['tracked_since'] is None
    assert empty['models'] == empty['sessions'] == empty['request_details'] == []
    assert [u['id'] for u in empty['identities']] == [bob]
    record(app, bob, '9', created_at='2026-09-30T23:59:59+00:00')
    record(app, bob, '0', created_at='2026-10-01T00:00:00+00:00')
    record(app, bob, None, status='pending')
    record(app, bob, None, status='failed', created_at='2026-10-07T23:59:59+00:00')
    record(app, bob, '10', created_at='2026-10-08T00:00:00+00:00')
    data = report(client)
    assert data['total']['spend'] == '0'
    assert data['total']['requests'] == 3
    assert data['priced_requests'] == data['total']['missing_costs'] == data['total']['pending_costs'] == 1
    for query in ['start=bad', 'start=2026-10-07&end=2026-10-01', 'start=2026-01-01&end=2026-12-31']:
        assert client.get('/api/spend?' + query).status_code == 422


def test_personal_request_limit_is_applied_after_user_scope(workspace):
    app, client = workspace
    alice = sign_in(app, client)
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    own, _ = record(app, bob, '0.5', created_at='2026-10-01T00:00:00+00:00')
    _, run = record(app, alice, '1')
    with app.state.store.connect() as conn:
        conn.executemany('''INSERT INTO model_requests(id,key_hash,run_id,user_id,model,created_at,cost)
            VALUES(?,'',?,?,'other-model','2026-10-07T12:00:00+00:00','1')''',
            [(f'other-{i}', run['id'], alice) for i in range(501)])
    assert [row['id'] for row in report(client)['request_details']] == [own]


def test_admin_full_report_and_role_changes_take_effect_without_new_login(users_app):
    app, client = users_app
    sign_as(app, client, 'tin@berri.ai')
    record(app, 'google:tin', '20')
    sign_as(app, client, 'maya@berri.ai')
    record(app, 'google:maya', '1')
    member_cookie = dict(client.cookies)
    assert report(client)['total']['spend'] == '1'
    sign_as(app, client, 'tin@berri.ai')
    assert change(client, 'maya@berri.ai', 'admin').status_code == 200
    admin_cookie = dict(client.cookies)
    client.cookies.clear(); client.cookies.update(member_cookie)
    full = report(client)
    assert full['scope'] == 'organization'
    assert full['total']['spend'] == '21'
    assert full['cost_summary']['llm'] == '21'
    assert 'infrastructure' in full
    assert full == client.get('/api/admin/spend?start=2026-10-01&end=2026-10-07').json()
    client.cookies.clear(); client.cookies.update(admin_cookie)
    assert change(client, 'maya@berri.ai', 'member', 1).status_code == 200
    client.cookies.clear(); client.cookies.update(member_cookie)
    personal = report(client)
    assert personal['scope'] == 'personal' and personal['total']['spend'] == '1'
    assert 'infrastructure' not in personal
    assert client.get('/api/admin/spend').status_code == 403
    assert client.post('/api/admin/spend/link-slack', json={
        'slack_user_id': 'slack:someone', 'google_user_id': 'google:maya'}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/spend').status_code == 401


def test_shared_member_password_cannot_expose_shared_spend(workspace):
    app, client = workspace
    app.state.settings.workspace_member_password = 'member-test-password'
    assert client.post('/api/login', json={'password': 'member-test-password'}).status_code == 200
    record(app, 'shared:password:member', '100')
    response = client.get('/api/spend')
    assert response.status_code == 403
    assert 'Sign in with your Google account' in response.json()['detail']



def test_daily_analytics_uses_full_scoped_ledger_and_zero_fills_days(workspace):
    app, client = workspace
    alice = sign_in(app, client)
    bob = sign_in(app, client, 'bob', 'bob@berri.ai')
    _, run = record(app, bob, '0.1', created_at='2026-10-01T00:00:00+00:00')
    with app.state.store.connect() as conn:
        conn.executemany('''INSERT INTO model_requests(id,key_hash,run_id,user_id,model,created_at,cost,status)
            VALUES(?,'',?,?,'own-model','2026-10-07T12:00:00+00:00','0.1','completed')''',
            [(f'own-{i}', run['id'], bob) for i in range(501)])
    record(app, bob, None, status='pending')
    record(app, bob, None, status='failed')
    record(app, alice, '999', model='private-model')
    data = report(client)
    assert len(data['request_details']) == 500
    assert len(data['daily']) == 7
    assert sum(d['requests'] for d in data['daily']) == data['total']['requests'] == 504
    assert sum(Decimal(d['spend']) for d in data['daily']) == Decimal(data['total']['spend']) == Decimal('50.2')
    assert data['daily'][1]['requests'] == data['daily'][1]['active_users'] == 0
    last = data['daily'][-1]
    assert last['active_users'] == 1
    assert last['pending_costs'] == last['missing_costs'] == 1
    assert all(m['model'] == 'own-model' for d in data['daily'] for m in d['models'])
    assert all(sum(Decimal(m['spend']) for m in d['models']) == Decimal(d['spend']) for d in data['daily'])
    assert 'private-model' not in json.dumps(data['daily'])
