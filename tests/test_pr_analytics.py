import asyncio
import json
from datetime import date

from app.pr_analytics import report
from app.db import Store
from app.session_pull_requests import SessionPullRequests
from test_pr_delivery import sidebar_github, pr_response
from test_spend import sign_in
from test_workspace import workspace


START, END = date(2026, 10, 1), date(2026, 10, 7)


def person(store: Store, name: str, kind: str = 'google', linked: str | None = None) -> str:
    identity = kind + ':' + name
    store.execute('''INSERT INTO users(id,kind,name,email,linked_user_id,created_at,updated_at)
        VALUES(?,?,?,?,?,'2026-09-01','2026-09-01')''', (identity, kind, name.title(), name+'@example.com', linked))
    return identity


def receipt(store: Store, run: dict[str, object], number: int, *, tracked: str = '2026-10-02T00:00:00+00:00', permanent: bool = True, suffix: str = '') -> None:
    data = {'repository': 'BerriAI/moyai', 'number': number, 'url': f'https://github.com/BerriAI/moyai/pull/{number}'}
    if permanent:
        data['repository_id'] = 202
    store.execute('''INSERT INTO github_publications
        (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
        VALUES(?,?,?,'fixture','fixture',?,'fixture',?)''',
        (run['id']+str(number)+suffix, run['id'], run['active_message_id'] or 0, json.dumps(data), tracked))


def cost(store: Store, run_id: str, identity: str, amount: str | None, status: str = 'completed') -> None:
    store.execute('''INSERT INTO model_requests(id,key_hash,run_id,user_id,model,created_at,cost,status)
        VALUES(?,'fixture',?,'','fixture','2026-09-01T00:00:00+00:00',?,?)''', (identity, run_id, amount, status))


async def refreshed_report(service: SessionPullRequests) -> dict[str, object]:
    report(service, START, END)
    await asyncio.gather(*list(service.pending.values()))
    return report(service, START, END)


async def test_pr_report_counts_merge_dates_credits_publication_actor_and_dedupes_family_spend(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    alice, bob = person(store, 'alice'), person(store, 'bob')
    slack = person(store, 'alice-slack', 'slack', alice)
    root = store.create_run('Shared session', '', 'demo', [], user_id=alice, chat_enabled=True)
    store.claim_message(root['id'])
    root = store.run(root['id'])
    child = store.create_run('Agent work', '', 'demo', [], user_id=slack, chat_enabled=True)
    store.claim_message(child['id'])
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root['id'], child['id']))
    child = store.run(child['id'])
    receipt(store, root, 100, permanent=False)
    receipt(store, child, 100)  # Duplicate permanent receipt does not change earliest actor.
    receipt(store, child, 101)
    store.execute("UPDATE runs SET active_user_id=?,owner_id=?,deleted_at='deleted' WHERE id=?", (bob, bob, root['id']))
    cost(store, root['id'], 'precise', '0.123456789012345678')
    cost(store, child['id'], 'child-cost', '0.2')
    cost(store, child['id'], 'pending', None, 'pending')
    cost(store, child['id'], 'missing', None)
    old = store.create_run('Earlier work', '', 'demo', [], user_id=bob)
    receipt(store, old, 102, tracked='2026-09-10T00:00:00+00:00')
    for i in range(502):
        cost(store, old['id'], 'old-'+str(i), '0.001')
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses.update({number: pr_response(number, state='closed', merged=True,
        created_at='2026-10-01T00:00:00Z' if number != 102 else '2026-09-10T00:00:00Z',
        merged_at='2026-10-07T23:59:59Z') for number in (100, 101, 102)})
    data = await refreshed_report(service)
    assert data['total_merged'] == 3 and data['total_created'] == 2 and data['contributors'] == 2
    assert [row['number'] for row in data['pull_requests']] == [101, 100]
    assert data['leaderboard'][0] == {'user_id': alice, 'name': 'Alice', 'email': 'alice@example.com',
        'created_prs': 2, 'status_counts': {'merged': 2, 'open': 0, 'draft': 0, 'closed': 0, 'unknown': 0},
        'merged_prs': 2, 'sessions': 1, 'spend': '0.323456789012345678', 'requests': 4,
        'pending_costs': 1, 'missing_costs': 1, 'cost_per_merged_pr': '0.161728394506172839'}
    assert data['leaderboard'][1]['spend'] == '0.502'
    assert data['leaderboard'][1]['created_prs'] == 0
    assert data['leaderboard'][1]['cost_per_merged_pr'] == '0.502'
    assert len(calls) == 3
    for row in data['pull_requests']:
        assert row['spend'] == '0.323456789012345678'
        assert row['user_id'] == alice
        assert row['sessions'] == [{'id': root['id'], 'title': 'Shared session', 'deleted': True}]
    await service.close()


async def test_created_statistics_use_verified_creation_dates_and_keep_people_without_merges(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    alice, bob = person(store, 'alice'), person(store, 'bob')
    earlier = store.create_run('Earlier PR, merged now', '', 'demo', [], user_id=alice)
    current = store.create_run('Current creations', '', 'demo', [], user_id=bob)
    for number in range(100, 106):
        receipt(store, current, number, tracked='2026-09-01T00:00:00+00:00')
    receipt(store, current, 100, suffix='-duplicate')
    receipt(store, earlier, 106)
    cost(store, earlier['id'], 'free-request', '0.00')
    service, _, responses = sidebar_github(app, monkeypatch)
    responses.update({
        100: pr_response(100, created_at='2026-09-30T17:00:00-07:00'),
        101: pr_response(101, draft=True, created_at='2026-10-07T16:59:59-07:00'),
        102: pr_response(102, state='closed', created_at='2026-10-02T00:00:00Z'),
        103: pr_response(103, state='closed', merged=True, created_at='2026-10-01T00:00:00Z',
                         merged_at='2026-10-09T00:00:00Z'),
        104: pr_response(104, created_at='2026-10-07T17:00:00-07:00'),
        105: pr_response(105, created_at=None),
        106: pr_response(106, state='closed', merged=True, created_at='2026-09-30T23:59:59Z',
                         merged_at='2026-10-03T00:00:00Z'),
    })
    data = await refreshed_report(service)
    assert data['total_created'] == 4 and data['total_merged'] == 1 and data['contributors'] == 2
    assert data['unknown_created_at'] == 1
    assert {pr['number'] for pr in data['created_pull_requests']} == {100, 101, 102, 103}
    assert {pr['number'] for pr in data['pull_requests']} == {106}
    winner, creator = data['leaderboard']
    assert winner['user_id'] == alice and winner['created_prs'] == 0
    assert winner['cost_per_merged_pr'] == '0.00'
    assert creator['user_id'] == bob and creator['created_prs'] == 4 and creator['merged_prs'] == 0
    assert creator['status_counts'] == {'merged': 1, 'open': 1, 'draft': 1, 'closed': 1, 'unknown': 0}
    assert creator['cost_per_merged_pr'] is None
    await service.close()


async def test_pr_report_unknown_and_shared_actors_remain_unattributed_and_merge_boundaries_are_utc(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    alice = person(store, 'alice')
    shared = person(store, 'password', 'shared')
    shared_run = store.create_run('Shared', '', 'demo', [], user_id=shared)
    missing_actor = store.create_run('Legacy chat', '', 'demo', [], user_id=alice, chat_enabled=True)
    for number, run in [(100, shared_run), (101, missing_actor), (102, shared_run), (103, shared_run), (104, shared_run)]:
        receipt(store, run, number)
    service, _, responses = sidebar_github(app, monkeypatch)
    dates = {100: '2026-09-30T17:00:00-07:00', 101: '2026-10-07T16:59:59-07:00',
             102: '2026-10-07T17:00:00-07:00', 103: None, 104: '2026-09-30T23:59:59Z'}
    responses.update({n: pr_response(n, state='closed', merged=True, merged_at=when) for n, when in dates.items()})
    data = await refreshed_report(service)
    assert data['total_merged'] == 2 and data['contributors'] == 0
    assert data['leaderboard'][0]['user_id'] == 'unattributed'
    assert {pr['number'] for pr in data['merged_pull_requests']} == {100, 101}
    assert data['unknown_status'] == 1
    await service.close()


async def test_pr_report_uses_current_connection_scope_and_ignores_forged_links(workspace, monkeypatch):
    from test_github import select
    app, _ = workspace
    store = app.state.store
    run = store.create_run('https://github.com/BerriAI/moyai/pull/999', '', 'demo', [])
    receipt(store, run, 100)
    receipt(store, run, 101)
    store.execute('UPDATE github_publications SET result=? WHERE id=?',
        ('{"repository":"BerriAI/moyai","number":101,"url":"https://evil.example/pull/101"}', run['id']+'101'))
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response(state='closed', merged=True, merged_at='2026-10-03T00:00:00Z')
    assert (await refreshed_report(service))['total_merged'] == 1
    select(app, (101,))
    data = report(service, START, END)
    assert data['total_merged'] == 0 and data['unknown_status'] == 1 and not data['pending_refresh']
    assert len(data['pull_requests']) == 1 and len(calls) == 1
    await service.close()


def test_pr_analytics_endpoint_is_admin_only_and_validates_dates(workspace):
    app, client = workspace
    sign_in(app, client)
    assert client.get('/api/admin/pull-requests?start=2026-10-01&end=2026-10-07').json()['total_merged'] == 0
    assert client.get('/api/admin/pull-requests?start=2026-01-01&end=2026-10-07').status_code == 422
    assert client.get('/api/admin/pull-requests?start=2026-10-08&end=2026-10-07').status_code == 422
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/pull-requests').status_code == 403
    assert 'pull_requests' not in client.get('/api/spend').json()
    client.cookies.clear()
    assert client.get('/api/admin/pull-requests').status_code == 401


async def test_ambiguous_legacy_receipt_never_borrows_a_reused_repository_name(workspace, monkeypatch):
    app, _ = workspace
    store = app.state.store
    run = store.create_run('Reused repository name', '', 'demo', [])
    receipt(store, run, 100)
    receipt(store, run, 100, suffix='-other')
    receipt(store, run, 100, permanent=False, suffix='-legacy')
    other = store.rows('SELECT result FROM github_publications WHERE id=?', (run['id']+'100-other',))[0]
    data = json.loads(other['result'])
    data['repository_id'] = 303
    store.execute('UPDATE github_publications SET result=? WHERE id=?', (json.dumps(data), run['id']+'100-other'))
    service, calls, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response(state='closed', merged=True, merged_at='2026-10-03T00:00:00Z')
    data = await refreshed_report(service)
    assert data['total_merged'] == 1 and data['unknown_status'] == 2
    assert len(data['pull_requests']) == 3 and len(calls) == 1
    await service.close()
