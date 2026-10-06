from datetime import date

from app.adoption import report
from test_workspace import workspace
from test_spend import sign_in


def submission(store, day='2026-10-05', actor='google:alice', mode='modal'):
    run = store.create_run('Adoption test fixture', '', mode, [], chat_enabled=True, user_id=actor)
    store.update_run(run['id'], status='idle')
    store.execute('UPDATE messages SET created_at=? WHERE run_id=?', (day + 'T12:00:00+00:00', run['id']))
    return run


def test_adoption_counts_submissions_not_calls_and_links_identities(workspace):
    app, client = workspace
    sign_in(app, client)
    store = app.state.store
    run = submission(store)
    store.enqueue_message(run['id'], 'Follow up', 'unique', user_id='google:alice')
    store.enqueue_message(run['id'], 'Follow up', 'unique', user_id='google:alice')
    store.execute("UPDATE messages SET created_at='2026-10-05T15:00:00+00:00' WHERE run_id=?", (run['id'],))
    with store.connect() as conn:
        slack = store.slack_identity_in(conn, 'TEAM', 'PERSON')
    store.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:alice', slack))
    submission(store, actor=slack)
    submission(store, actor='')
    submission(store, mode='demo')
    child = submission(store)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], child['id']))
    store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','Answer','completed','2026-10-05T16:00:00+00:00')", (run['id'],))
    app.state.spend.begin(store.run(run['id']), 'test-model')
    data = report(store, date(2026, 10, 4), date(2026, 10, 6), today=date(2026, 10, 6))
    assert data['total_requests'] == 4
    assert data['active_users'] == 1
    assert [d['requests'] for d in data['daily']] == [0, 4, 0]
    assert data['daily'][-1]['partial'] is True
    assert data['daily'][1]['seven_day_average'] == 0.57
    assert 'content' not in str(data) and 'alice' not in str(data)


def test_automation_initial_excluded_but_human_followup_included(workspace):
    app, _ = workspace
    store = app.state.store
    actor = store.identity({'method': 'local'})
    run = submission(store, actor=actor)
    store.execute("INSERT INTO automations(id,owner_id,definition,created_at,updated_at) VALUES('a',?,'{}','2026-10-05','2026-10-05')", (actor,))
    store.execute("INSERT INTO automation_runs(occurrence,automation_id,revision,run_id,outcome,created_at) VALUES('o','a',1,?,'started','2026-10-05')", (run['id'],))
    store.enqueue_message(run['id'], 'Human followup', 'human', user_id=actor)
    store.execute("UPDATE messages SET created_at='2026-10-05T12:00:00+00:00' WHERE run_id=?", (run['id'],))
    data = report(store, date(2026, 10, 5), date(2026, 10, 5), today=date(2026, 10, 6))
    assert data['total_requests'] == 1
    assert data['active_users'] == 0  # A shared login is not a distinct teammate.


def test_weekly_uses_complete_days_and_average_reads_before_range(workspace):
    app, _ = workspace
    for day in ['2026-09-28', '2026-10-04', '2026-10-05', '2026-10-06']:
        submission(app.state.store, day=day)
    data = report(app.state.store, date(2026, 10, 5), date(2026, 10, 6), today=date(2026, 10, 6))
    assert data['weekly'] == {'start': '2026-09-29', 'end': '2026-10-05', 'previous_start': '2026-09-22',
                              'previous_end': '2026-09-28', 'requests': 2, 'previous_requests': 1, 'delta': 1, 'percent_change': 100.0}
    assert data['daily'][0]['seven_day_average'] == 0.29


def test_utc_boundaries_null_client_and_failed_deleted_submissions(workspace):
    app, _ = workspace
    store = app.state.store
    run = submission(store)
    store.execute("UPDATE messages SET created_at='2026-10-06T01:00:00+02:00', client_id=NULL, status='deleted' WHERE run_id=?", (run['id'],))
    other = submission(store)
    store.execute("UPDATE messages SET created_at='2026-10-06T00:00:00+00:00', status='failed' WHERE run_id=?", (other['id'],))
    data = report(store, date(2026, 10, 5), date(2026, 10, 6), today=date(2026, 10, 6))
    assert [d['requests'] for d in data['daily']] == [1, 1]


def test_empty_range_and_admin_access(workspace):
    app, client = workspace
    sign_in(app, client)
    result = client.get('/api/admin/adoption')
    assert result.status_code == 200
    data = result.json()
    assert len(data['daily']) == 30
    assert data['total_requests'] == data['active_users'] == 0
    assert data['weekly']['percent_change'] is None
    for query in ['start=no', 'start=2026-10-05&end=2026-10-04', 'start=2025-01-01&end=2026-01-01', 'end=2099-01-01']:
        assert client.get('/api/admin/adoption?' + query).status_code == 422
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/adoption').status_code == 403
    client.cookies.clear()
    assert client.get('/api/admin/adoption').status_code == 401
