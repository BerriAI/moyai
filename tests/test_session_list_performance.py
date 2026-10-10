import asyncio
from contextlib import contextmanager
import json
from threading import Event, Timer

import httpx
import pytest

from app.db import now
from test_pr_delivery import pr_response, publication, sidebar_github
from test_user_roles import sign_as, users_app


def seed(store, owner, count):
    stamp = now()
    with store.connect() as conn:
        conn.executemany('''INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,
            created_at,updated_at,owner_id,chat_enabled,summary)
            VALUES(?,?,'','demo','idle','[]',?,?,?,1,?)''',
            [(f'{index + 1:032x}', f'Task {index + 1}', stamp, stamp, owner,
              'Large saved answer. ' * 2000) for index in range(count)])


def test_list_query_count_stays_bounded_and_full_detail_keeps_answers(users_app, monkeypatch):
    app, client = users_app
    sign_as(app, client, 'tin@berri.ai')
    store = app.state.store
    seed(store, 'google:tin', 100)
    queries = []
    connect = store.connect

    @contextmanager
    def counted():
        with connect() as conn:
            if store.database:
                execute = conn.raw.execute

                def traced(sql, *args, **kwargs):
                    queries.append(sql)
                    return execute(sql, *args, **kwargs)

                # Include Postgres ownership/transaction queries in the bound,
                # and restore the pooled connection before another request.
                with monkeypatch.context() as tracing:
                    tracing.setattr(conn.raw, 'execute', traced)
                    yield conn
            else:
                conn.set_trace_callback(queries.append)
                yield conn

    monkeypatch.setattr(store, 'connect', counted)
    response = client.get('/api/runs?scope=all&view=sidebar')
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 100
    # Includes authentication, identity registration, metadata, PR receipts and
    # agent trees. A per-session read would exceed this even with only 100 rows.
    assert len(queries) < 40
    assert len(response.content) < 150_000
    assert all('summary' not in row and 'pending_result' not in row and 'token_hash' not in row for row in rows)
    assert [row['id'] for row in rows] == [f'{index:032x}' for index in range(100, 0, -1)]
    full = client.get('/api/runs?scope=all').json()
    assert len(full[0]['summary']) == 40_000
    assert full[0]['plugins'] == []
    assert {key: full[0][key] for key in rows[0]} == rows[0]
    detail = client.get('/api/runs/' + rows[0]['id']).json()
    assert detail['summary'] == full[0]['summary']


@pytest.mark.parametrize('view', ['full', 'sidebar'])
def test_list_retains_status_tree_scope_archive_search_and_focus(users_app, view):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    store = app.state.store
    seed(store, 'google:maya', 105)
    root = f'{1:032x}'
    child = store.create_run('Nested worker', '', 'demo', [], user_id='google:maya')
    message = store.create_run('Completed pending answer', '', 'demo', [], user_id='google:maya')
    store.execute("UPDATE runs SET parent_run_id=?,agent_label='Research',status='running',active_message_id=123,pending_result=? WHERE id=?",
                  (root, json.dumps({'message_id': 123, 'completed': True, 'message': 'Finished'}), child['id']))
    store.execute("UPDATE runs SET parent_run_id=?,agent_label='Grandchild',status='running',deletion_requested_at=? WHERE id=?",
                  (child['id'], now(), message['id']))
    store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','needle-in-history','completed',?)", (child['id'], now()))
    query = {'scope': 'mine', 'view': view, 'focus': child['id']}
    rows = client.get('/api/runs', params=query).json()
    family = next(row for row in rows if row['id'] == root)
    assert len(rows) == 101  # Focus keeps an older family reachable.
    assert family['can_delete'] is True and family['archived'] is False
    assert family['children'][0]['status'] == 'saving'
    assert family['children'][0]['children'][0]['status'] == 'deleting'
    assert 'summary' not in family['children'][0]
    app.state.session_lifecycle.deletion_errors[root] = 'Cleanup is retrying.'
    folder = client.post('/api/session-folders', json={'name': 'Filed'}).json()['id']
    assert client.put(f'/api/runs/{root}/folder', json={'folder_id': folder}).status_code == 200
    assert client.put(f'/api/runs/{root}/pin', json={'pinned': True}).status_code == 200
    rows = client.get('/api/runs', params={'scope': 'mine', 'view': view, 'search': 'needle-in-history'}).json()
    assert len(rows) == 1 and rows[0]['id'] == root
    assert rows[0]['folder_id'] == folder and rows[0]['pinned'] is True
    assert rows[0]['deletion_error'] == 'Cleanup is retrying.'
    assert rows[0]['children'][0]['search_match'] is True
    assert client.post(f'/api/runs/{root}/archive', json={'archived': True}).status_code == 200
    assert root not in {row['id'] for row in client.get('/api/runs', params=query).json()}
    assert client.get('/api/runs', params={**query, 'archived': True}).json()[0]['id'] == root
    assert client.get('/api/runs', params={'scope': 'all', 'view': view}).status_code == 403
    sign_as(app, client, 'other@berri.ai')
    assert client.get('/api/runs', params=query).json() == []
    sign_as(app, client, 'tin@berri.ai')
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), root))
    assert root not in {row['id'] for row in client.get('/api/runs', params={**query, 'scope': 'all'}).json()}


def test_bulk_rows_keep_order_across_batches_and_empty_input(users_app):
    app, client = users_app
    sign_as(app, client, 'maya@berri.ai')
    store = app.state.store
    seed(store, 'google:maya', 405)
    ids = [f'{index:032x}' for index in range(405, 0, -1)]
    assert store.runs_by_ids([], sidebar=True) == []
    assert [row['id'] for row in store.runs_by_ids(ids + ['missing'], sidebar=True)] == ids


async def test_slow_list_database_read_does_not_stall_other_requests(users_app, monkeypatch):
    app, client = users_app
    sign_as(app, client, 'tin@berri.ai')
    entered, release = Event(), Event()
    original = app.state.store.sidebar_run_ids

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3), 'Timed out waiting for test release'
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.store, 'sidebar_run_ids', held)

    @app.get('/test/session-list-ping')
    async def ping():
        return {'ready': True}

    # A fallback release makes a regression fail rather than hang the runner.
    fallback = Timer(2, release.set)
    fallback.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                base_url=str(client.base_url), cookies=client.cookies) as reader:
        pending = asyncio.create_task(reader.get('/api/runs?view=sidebar'))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            response = await reader.get('/test/session-list-ping')
            assert response.json() == {'ready': True}
            assert not release.is_set(), 'Session-list SQL blocked the event loop'
        finally:
            release.set()
            fallback.cancel()
            assert (await pending).status_code == 200


async def test_sidebar_receipts_still_schedule_pr_refreshes_on_event_loop(users_app, monkeypatch):
    app, client = users_app
    sign_as(app, client, 'tin@berri.ai')
    run = app.state.store.create_run('PR session', '', 'demo', [], user_id='google:tin')
    publication(app, run['id'], 100)
    unused, calls, responses = sidebar_github(app, monkeypatch)
    responses[100] = pr_response()
    service = app.state.session_pull_requests
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                base_url=str(client.base_url), cookies=client.cookies) as reader:
        first = await reader.get('/api/runs?view=sidebar&scope=all')
        assert first.status_code == 200
        assert first.json()[0]['pr_summary']['unknown'] == 1
        await asyncio.gather(*list(service.pending.values()))
        second = await reader.get('/api/runs?view=sidebar&scope=all')
        assert second.json()[0]['pr_summary']['open'] == 1
        assert second.json()[0]['pr_summary']['label'] == 'PR is ready'
        assert len(calls) == 1
    await unused.close()


@pytest.mark.parametrize('operation', ['create', 'detail'])
async def test_startup_authorization_reads_do_not_block_other_requests(users_app, monkeypatch, operation):
    app, client = users_app
    sign_as(app, client, 'tin@berri.ai')
    run = app.state.store.create_run('Existing session', '', 'demo', [], user_id='google:tin')
    entered, release = Event(), Event()
    original = app.state.security.require

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.security, 'require', held)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)

    @app.get('/test/startup-ping')
    async def ping():
        return {'ready': True}

    fallback = Timer(2, release.set)
    fallback.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=str(client.base_url),
                                cookies=client.cookies, headers=client.headers) as reader:
        request = (reader.post('/api/runs', json={'prompt': 'Create a session', 'mode': 'demo'})
                   if operation == 'create' else reader.get('/api/runs/' + run['id']))
        pending = asyncio.create_task(request)
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert (await reader.get('/test/startup-ping')).json() == {'ready': True}
            assert not release.is_set(), 'Startup authorization SQL blocked the event loop'
        finally:
            release.set()
            fallback.cancel()
            assert (await pending).status_code == (201 if operation == 'create' else 200)
