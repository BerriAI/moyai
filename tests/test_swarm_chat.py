"""The shared room reads bounded public speech from the real durable subtree."""
import asyncio
import json
from uuid import uuid4

import pytest

from agent.swarm.planning import INITIAL_TEAM_SIZE
from app.db import now
from app.swarm_chat import MAX_CONTENT_CHARACTERS
from app.swarm_collaboration import Assign, ClaimLead, Publish
from test_swarm_time_controls import roster, start_swarm, swarm_api


def chat(client, root, **params):
    response = client.get(f'/api/runs/{root}/swarm/chat', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def add_message(store, root, content, *, role='assistant', status='completed', stamp=None,
                client_id=None, input_kind='user', response_to_id=None):
    with store.connect() as conn:
        conn.begin_write()
        return conn.execute('''INSERT INTO messages(run_id,role,content,status,created_at,model,user_id,
            client_id,input_kind,response_to_id) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id''',
            (root, role, content, status, stamp or now(), 'recorded-model', 'google:alice',
             client_id, input_kind, response_to_id)).fetchone()[0]


def add_post(store, member, content, *, stamp=None, group_id=None, target_id='', kind='message'):
    run = store.run(member)
    with store.connect() as conn:
        conn.begin_write()
        return conn.execute('''INSERT INTO swarm_posts(group_id,member_id,kind,target_id,content,dedupe_key,created_at)
            VALUES(?,?,?,?,?,?,?) RETURNING id''', (group_id or run['agent_group_id'], member, kind,
            target_id, content, uuid4().hex, stamp or now())).fetchone()[0]


def nested_run(store, parent, label='Nested researcher'):
    run = store.create_run('Private delegation prompt', '', 'modal', [], chat_enabled=True,
                           user_id='google:alice', model='recorded-model')
    store.execute('UPDATE runs SET parent_run_id=?,agent_label=? WHERE id=?', (parent, label, run['id']))
    return run['id']


def test_real_public_board_and_responses_have_actual_authors(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    members = roster(app, root)
    assert len(members) == INITIAL_TEAM_SIZE
    lead, worker = [row['id'] for row in members[:2]]
    for identity in (lead, worker):
        store.claim_message(identity)
        store.update_run(identity, status='running')
    group = store.run(lead)['agent_group_id']
    store.execute("UPDATE agent_groups SET status='running' WHERE id=?", (group,))
    service = app.state.coordinator.collaboration
    service.claim_lead(store.run(lead), ClaimLead(plan='I will coordinate the investigation.'))
    assignment = service.assign(store.run(lead), Assign(member_id=worker, task='Compare deployment tradeoffs.'))
    published = service.publish(store.run(worker), Publish(message='A durable room keeps the work visible.'))
    user_turn = store.claim_message(root)
    store.finish_message(root, user_turn['id'], 'The team has started.')
    worker_turn = store.run(worker)['active_message_id']
    store.finish_message(worker, worker_turn, 'Here is the public comparison.')
    answer_model = store.rows('SELECT model FROM messages WHERE run_id=? AND response_to_id=?', (worker, worker_turn))[0]['model']
    store.execute('UPDATE runs SET model=? WHERE id=?', ('future-model-preference', worker))
    nested = nested_run(store, worker)
    nested_answer = add_message(store, nested, 'A nested public conclusion.')
    result = chat(client, root)
    items = result['items']
    human = next(item for item in items if item['kind'] == 'human')
    assert human['author'] == {'id': 'google:alice', 'name': 'Alice', 'role': 'human',
                               'harness': None, 'model': None, 'is_leader': False}
    assert human['run_id'] == root
    plan = next(item for item in items if item['kind'] == 'leadership')
    assert plan['author']['id'] == lead and plan['author']['is_leader']
    assert plan['author']['harness'] == store.run(lead)['harness']
    assert plan['author']['model'] is None  # Historical board posts did not record the turn model.
    task = next(item for item in items if item['id'] == f"board:{assignment['post_id']}")
    assert task['target'] == {'id': worker, 'name': store.run(worker)['agent_label']}
    assert sum(item['id'] == f"board:{published['post_id']}" for item in items) == 1
    reply = next(item for item in items if item['id'] == f'response:{worker}:{worker_turn}')
    assert reply['content'] == 'Here is the public comparison.'
    assert reply['author']['role'] == 'agent'
    assert reply['author']['model'] == answer_model
    assert any(item['id'] == f'message:{nested_answer}' for item in items)
    assert sum(item['kind'] == 'human' for item in items) == 1


def test_feed_excludes_private_inputs_events_and_unrelated_or_deleted_subtrees(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    unrelated = start_swarm(client)
    store = app.state.store
    child, deleted = [row['id'] for row in roster(app, root)[:2]]
    foreign = roster(app, unrelated)[0]['id']
    nested = nested_run(store, deleted)
    for run_id, text in ((root, 'Visible root'), (child, 'Visible worker'), (unrelated, 'SECRET foreign'),
                         (foreign, 'SECRET foreign worker'), (deleted, 'SECRET deleted'), (nested, 'SECRET deleted ancestor')):
        add_message(store, run_id, text)
    add_message(store, child, 'SECRET assignment', role='user')
    add_message(store, root, 'SECRET wake', role='user', client_id='swarm:continue:1')
    add_message(store, root, 'SECRET Slack observation', role='user', input_kind='observation')
    for role in ('tool', 'system', 'developer'):
        add_message(store, root, 'SECRET ' + role, role=role)
    for status in ('deleted', 'steered'):
        add_message(store, child, 'SECRET ' + status, status=status)
    add_message(store, root, '  ')
    store.event(root, 'reasoning', 'SECRET hidden thought', {'content': 'SECRET trace'})
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), deleted))
    add_post(store, child, 'Visible board')
    add_post(store, foreign, 'SECRET forged board', group_id=store.run(child)['agent_group_id'])
    add_post(store, child, 'SECRET deleted target', target_id=deleted)
    add_post(store, child, 'SECRET unknown kind', kind='reasoning')
    result = chat(client, root)
    assert 'SECRET' not in json.dumps(result)
    contents = {item['content'] for item in result['items']}
    assert {'Visible root', 'Visible worker', 'Visible board'} <= contents
    assert len(contents) == 4  # Initial owner task plus the three public posts.


def test_latest_history_and_incremental_reconnect_do_not_skip_timestamp_ties(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    member = roster(app, root)[0]['id']
    stamp = '2030-01-01T00:00:00+00:00'
    expected = []
    for index in range(9):
        expected += [f'message:{add_message(store, member, str(index), stamp=stamp)}',
                     f'board:{add_post(store, member, str(index), stamp=stamp)}']
    first = chat(client, root, limit=3)
    assert len(first['items']) == 3 and first['has_more']
    history = list(first['items'])
    page = first
    while page['has_more']:
        page = chat(client, root, before=page['previous_cursor'], limit=3)
        history = page['items'] + history
    assert len(history) == len({item['id'] for item in history}) == 19
    assert set(expected) <= {item['id'] for item in history}
    assert [item['created_at'] for item in history] == sorted(item['created_at'] for item in history)
    assert chat(client, root, after=first['next_cursor'])['items'] == []
    late_board = add_post(store, member, 'Late board same timestamp', stamp=stamp)
    backdated_message = add_message(store, root, 'Late import', stamp='2000-01-01T00:00:00+00:00')
    page = chat(client, root, after=first['next_cursor'], limit=1)
    replay = chat(client, root, after=first['next_cursor'], limit=1)
    assert replay == page
    arrived = list(page['items'])
    while page['has_more']:
        page = chat(client, root, after=page['next_cursor'], limit=1)
        arrived += page['items']
    assert {item['id'] for item in arrived} == {f'board:{late_board}', f'message:{backdated_message}'}
    assert chat(client, root, after=page['next_cursor'])['items'] == []


def test_earlier_page_is_a_snapshot_and_new_arrivals_remain_in_forward_stream(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    for index in range(5):
        add_message(store, root, str(index))
    first = chat(client, root, limit=2)
    late = add_message(store, root, 'Late arrival', stamp='2000-01-01T00:00:00+00:00')
    older = chat(client, root, before=first['previous_cursor'])
    assert f'message:{late}' not in {item['id'] for item in older['items']}
    assert chat(client, root, after=first['next_cursor'])['items'][0]['id'] == f'message:{late}'


def test_public_receipt_keeps_identity_when_the_canonical_response_arrives(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    worker = roster(app, root)[0]['id']
    turn = store.claim_message(worker)
    store.update_run(worker, status='running', summary='Public answer while saving', pending_result=json.dumps({
        'message_id': turn['id'], 'completed': True, 'message': 'Public answer while saving',
        'reasoning': 'SECRET private work', 'tool_result': 'SECRET tool result',
    }))
    first = chat(client, root)
    identity = f"response:{worker}:{turn['id']}"
    live = next(item for item in first['live_items'] if item['id'] == identity)
    assert live['status'] == 'saving'
    assert 'SECRET' not in json.dumps(first)
    store.finish_message(worker, turn['id'], 'Public answer while saving')
    store.update_run(worker, status='idle', pending_result='')
    next_page = chat(client, root, after=first['next_cursor'], watch=identity)
    assert next_page['items'][0]['id'] == identity
    assert next_page['updated_items'][0]['id'] == identity
    assert next_page['updated_items'][0]['status'] == 'completed'
    assert not next_page['removed_ids']
    assert next_page['live_items'] == []  # Settled answers are not retransmitted on every poll.


@pytest.mark.parametrize('status', ['completed', 'save_failed', 'steered', 'deleted'])
def test_watched_mutable_answer_settles_after_the_next_turn_has_started(swarm_api, status):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    worker = roster(app, root)[0]['id']
    turn = store.claim_message(worker)
    with store.connect() as conn:
        conn.begin_write()
        store.save_answer_in(conn, worker, turn['id'], 'Initial saving answer', 'saving')
    identity = f"response:{worker}:{turn['id']}"
    first = chat(client, root)
    assert next(item for item in first['items'] if item['id'] == identity)['status'] == 'saving'
    store.finish_message(worker, turn['id'], 'Final revised answer', status=status)
    store.update_run(worker, status='idle', pending_result='')
    store.enqueue_message(worker, 'Another instruction', 'next-' + uuid4().hex, user_id='google:alice')
    assert store.claim_message(worker)['id'] != turn['id']
    result = chat(client, root, after=first['next_cursor'], watch=identity)
    if status in {'completed', 'save_failed'}:
        assert result['updated_items'][0]['content'] == 'Final revised answer'
        assert result['updated_items'][0]['status'] == status
        assert not result['removed_ids']
    else:
        assert result['removed_ids'] == [identity]
        assert not result['updated_items']


def test_watch_is_scoped_and_updates_human_edits_without_new_message_ids(swarm_api):
    app, client = swarm_api
    root, unrelated = start_swarm(client), start_swarm(client)
    store = app.state.store
    foreign = add_message(store, unrelated, 'SECRET other room')
    first = chat(client, root)
    human = next(item for item in first['items'] if item['kind'] == 'human')
    source_id = int(human['id'].split(':')[1])
    store.execute('UPDATE messages SET content=? WHERE id=?', ('Edited owner instruction', source_id))
    watched = human['id'] + f',message:{foreign}'
    result = chat(client, root, after=first['next_cursor'], watch=watched)
    assert result['updated_items'][0]['content'] == 'Edited owner instruction'
    assert result['removed_ids'] == [f'message:{foreign}']
    assert 'SECRET' not in json.dumps(result)


def test_feed_bounds_payloads_and_validates_all_pagination_inputs(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    for index in range(104):
        add_message(store, root, str(index))
    big = add_message(store, root, 'x' * (MAX_CONTENT_CHARACTERS + 20))
    page = chat(client, root, limit=100)
    assert len(page['items']) == 100 and page['has_more']
    long_item = next(item for item in page['items'] if item['id'] == f'message:{big}')
    assert len(long_item['content']) == MAX_CONTENT_CHARACTERS and long_item['truncated']
    for params in ({'limit': 0}, {'limit': 101}, {'after': 'not-json'}, {'before': page['next_cursor']},
                   {'after': page['next_cursor'], 'before': page['previous_cursor']},
                   {'watch': 'response:invalid:123'}, {'watch': ','.join(['message:1'] * 101)},
                   {'watch': 'message:9999999999999999999'}):
        assert client.get(f'/api/runs/{root}/swarm/chat', params=params).status_code == 422
    other = start_swarm(client)
    assert client.get(f'/api/runs/{other}/swarm/chat', params={'after': page['next_cursor']}).status_code == 422


def test_feed_requires_session_auth_and_a_live_root_swarm(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    ordinary = app.state.store.create_run('Ordinary task', '', 'modal', [])['id']
    for identity in (ordinary, roster(app, root)[0]['id'], 'f' * 32):
        assert client.get(f'/api/runs/{identity}/swarm/chat').status_code == 404
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.get(f'/api/runs/{root}/swarm/chat').status_code == 200
    client.headers['X-CSRF-Token'] = csrf
    app.state.store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), root))
    assert client.get(f'/api/runs/{root}/swarm/chat').status_code == 404
    client.cookies.clear()
    assert client.get(f'/api/runs/{root}/swarm/chat').status_code == 401


def test_feed_runs_database_projection_off_the_event_loop(swarm_api, monkeypatch):
    import app.swarm_chat as projection
    app, client = swarm_api
    root = start_swarm(client)
    original = projection.read_page
    calls = []
    def checked(*args, **kwargs):
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(projection, 'read_page', checked)
    chat(client, root)
    assert calls == [True]


def test_delivery_sequence_handles_postgres_transactions_committing_out_of_id_order(swarm_api):
    app, client = swarm_api
    store = app.state.store
    if not store.database:
        pytest.skip('PostgreSQL allows independent session transactions to commit out of allocation order.')
    root = start_swarm(client)
    child_a, child_b = [row['id'] for row in roster(app, root)[:2]]
    turn_a, turn_b = store.claim_message(child_a), store.claim_message(child_b)
    initial = chat(client, root)
    with store.connect(write_scope=child_a) as early:
        early.begin_write()
        store.save_answer_in(early, child_a, turn_a['id'], 'The earlier ID commits last.', 'completed')
        early_id = early.execute('SELECT id FROM messages WHERE run_id=? AND response_to_id=?',
                                 (child_a, turn_a['id'])).fetchone()[0]
        with store.connect(write_scope=child_b) as later:
            later.begin_write()
            store.save_answer_in(later, child_b, turn_b['id'], 'The later ID commits first.', 'completed')
            later_id = later.execute('SELECT id FROM messages WHERE run_id=? AND response_to_id=?',
                                     (child_b, turn_b['id'])).fetchone()[0]
        assert later_id > early_id
        # This read must not acquire the held member write lock or wait for it.
        visible = chat(client, root, after=initial['next_cursor'])
        assert [item['id'] for item in visible['items']] == [f"response:{child_b}:{turn_b['id']}"]
    following = chat(client, root, after=visible['next_cursor'])
    assert [item['id'] for item in following['items']] == [f"response:{child_a}:{turn_a['id']}"]
    assert chat(client, root, after=following['next_cursor'])['items'] == []


def test_large_projection_backfill_is_bounded_resumable_and_opens_latest_history(swarm_api, monkeypatch):
    import app.swarm_chat as projection
    app, client = swarm_api
    store = app.state.store
    root = start_swarm(client)
    monkeypatch.setattr(projection, 'PROJECTION_BATCH_SIZE', 5)
    monkeypatch.setattr(projection, 'MAX_PROJECTION_BATCHES', 2)
    ids = [add_message(store, root, f'Public response {index}') for index in range(14)]
    response = client.get(f'/api/runs/{root}/swarm/chat', params={'limit': 3})
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
    assert response.json()['detail'] == projection.PREPARING_HISTORY
    assert len(store.rows('SELECT id FROM swarm_chat_entries WHERE root_id=?', (root,))) == 10
    page = chat(client, root, limit=3)
    assert [item['id'] for item in page['items']] == [f'message:{identity}' for identity in ids[-3:]]
    assert page['has_more']
    entries = store.rows('SELECT * FROM swarm_chat_entries WHERE root_id=? ORDER BY id', (root,))
    assert len(entries) == 15
    assert all(set(entry) == {'id', 'root_id', 'run_id', 'source', 'source_id', 'created_at'} for entry in entries)
    generation = store.generation
    assert chat(client, root, after=page['next_cursor'])['items'] == []
    assert store.rows('SELECT * FROM swarm_chat_entries WHERE root_id=? ORDER BY id', (root,)) == entries
    assert store.generation == generation  # An unchanged poll does not manufacture a write.


def test_concurrent_projectors_deduplicate_and_still_filter_current_deleted_sources(swarm_api):
    from concurrent.futures import ThreadPoolExecutor
    from app.main import public_messages
    from app.swarm_chat import read_page
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    child = roster(app, root)[0]['id']
    ids = [add_message(store, child, f'Public response {index}') for index in range(8)]
    post_id = add_post(store, child, 'Public board post')
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda _: read_page(store, root, public_messages), range(4)))
    assert pages == [pages[0]] * 4
    assert len(store.rows('SELECT id FROM swarm_chat_entries WHERE root_id=?', (root,))) == 10
    store.execute("UPDATE messages SET status='deleted' WHERE id=?", (ids[0],))
    store.execute('DELETE FROM swarm_posts WHERE id=?', (post_id,))
    page = chat(client, root, watch=f'message:{ids[0]}')
    assert page['removed_ids'] == [f'message:{ids[0]}']
    assert f'message:{ids[0]}' not in {item['id'] for item in page['items']}
    assert f'board:{post_id}' not in {item['id'] for item in page['items']}
    store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), child))
    assert [item['kind'] for item in chat(client, root)['items']] == ['human']


def test_projection_write_requires_auth_and_valid_room_and_cursor(swarm_api):
    app, client = swarm_api
    root = start_swarm(client)
    store = app.state.store
    assert client.get(f'/api/runs/{root}/swarm/chat', params={'after': 'invalid'}).status_code == 422
    assert client.get(f"/api/runs/{roster(app, root)[0]['id']}/swarm/chat").status_code == 404
    assert store.rows('SELECT * FROM swarm_chat_entries') == []
    client.cookies.clear()
    assert client.get(f'/api/runs/{root}/swarm/chat').status_code == 401
    assert store.rows('SELECT * FROM swarm_chat_entries') == []
