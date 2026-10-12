"""Conversation detail keeps complete metadata with bounded database reads."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

import pytest

from app.db import now
from test_cloudflare_access import access_key
from test_cloudflare_login import login


def short_conversation(store, owner):
    run = store.create_run('Read the saved chat', '', 'demo', [], chat_enabled=True, user_id=owner, harness='hermes')
    for index in range(3):
        if index:
            store.enqueue_message(run['id'], f'Follow up {index}', f'turn-{index}', user_id=owner)
        message = store.claim_message(run['id'])
        store.finish_message(run['id'], message['id'], f'Saved answer {index}')
        store.update_run(run['id'], status='idle')
    return run


def test_conversation_metadata_read_budget(login, monkeypatch):
    app, client = login
    owner = client.get('/api/session').json()['user_id']
    store = app.state.store
    run = short_conversation(store, owner)
    queries, checkouts = [], []
    measuring = ContextVar('conversation_read_budget', default=False)
    connect = store.connect

    @contextmanager
    def counted(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            if not measuring.get():
                yield conn
                return
            checkouts.append(1)
            if store.database:
                execute = conn.raw.execute

                def traced(sql, *args, **kwargs):
                    queries.append(sql)
                    return execute(sql, *args, **kwargs)

                with monkeypatch.context() as tracing:
                    tracing.setattr(conn.raw, 'execute', traced)
                    yield conn
            else:
                conn.set_trace_callback(queries.append)
                yield conn

    monkeypatch.setattr(store, 'connect', counted)
    token = measuring.set(True)
    try:
        response = client.get(f'/api/runs/{run["id"]}?activity=summary')
    finally:
        measuring.reset(token)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert len(detail['messages']) == 6
    assert [row['content'] for row in detail['messages'] if row['role'] == 'assistant'] == [
        f'Saved answer {index}' for index in range(3)]
    assert detail['owner']['id'] == owner
    assert detail['workflow_root_id'] == run['id']
    assert detail['can_delete'] is True and detail['archived'] is False
    assert {'goal', 'approvals', 'agents', 'pr_write_access', 'credential_requests',
            'slack_mirroring', 'has_captures', 'has_artifact', 'has_published_artifacts'} <= detail.keys()
    # The previous path needed 35 data reads and 29 checkouts for the same six
    # messages. Two narrow privacy admission reads now fit alongside the
    # metadata. PostgreSQL retains its original budget by sharing checkouts;
    # it also checks its schema owner on every checkout.
    reads = [sql for sql in queries if sql.lstrip().upper().startswith(('SELECT', 'WITH'))]
    assert 0 < len(reads) <= (40 if store.database else 28), queries
    assert len(checkouts) <= 14


def test_child_metadata_keeps_root_labels_and_exact_session_owner(login):
    app, client = login
    actor = client.get('/api/session').json()['user_id']
    store = app.state.store
    owner = store.identity({'method': 'google', 'identity': {
        'sub': 'maya', 'email': 'maya@berri.ai', 'name': 'Maya'}})
    root = store.create_run('Shared parent', '', 'demo', [], user_id=owner, harness='hermes')
    child = short_conversation(store, owner)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root['id'], child['id']))
    with store.connect() as conn:
        slack_actor = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute('UPDATE users SET linked_user_id=? WHERE id=?', (actor, slack_actor))
        # Contributions from linked Slack accounts count even after deletion.
        conn.execute("UPDATE messages SET user_id=?,status='deleted' WHERE run_id=? AND role='user'",
                     (slack_actor, child['id']))
        conn.execute('INSERT INTO session_archives VALUES(?,?,?)', (actor, root['id'], now()))
        conn.execute('INSERT INTO session_pins VALUES(?,?,?)', (actor, root['id'], now()))
        conn.execute('''INSERT INTO published_artifacts VALUES(?, 'proof.txt', 'revision', 'upload',
            5, 'text/plain', ?, NULL)''', (child['id'], now()))
    app.state.session_lifecycle.deletion_errors[root['id']] = 'Parent cleanup is retrying.'
    detail = client.get('/api/runs/' + child['id']).json()
    assert detail['owner'] == {'id': owner, 'email': 'maya@berri.ai', 'name': 'Maya'}
    assert detail['workflow_root_id'] == root['id']
    assert detail['archived'] is True and detail['pinned'] is True
    assert detail['participated'] is True and detail['slack_connected'] is False
    assert detail['can_delete'] is False
    assert detail['deletion_error'] == 'Parent cleanup is retrying.'
    assert detail['has_published_artifacts'] is True
    assert client.get('/api/runs/' + root['id']).json()['has_published_artifacts'] is False
    assert {key: detail[key] for key in ('pinned', 'participated', 'slack_connected')} == (
        store.sidebar_metadata(actor, [root['id']])[root['id']])
    with store.connect() as conn:
        metadata = store.conversation_metadata_in(conn, store.run(child['id'], connection=conn), slack_actor)
    assert metadata['feedback_author'] == 'tin@berri.ai'
    # No cross-request cache may retain pin/archive/profile changes.
    store.execute('DELETE FROM session_pins WHERE owner_id=?', (actor,))
    store.execute('DELETE FROM session_archives WHERE owner_id=?', (actor,))
    store.execute("UPDATE users SET name='Maya Updated' WHERE id=?", (owner,))
    fresh = client.get('/api/runs/' + child['id']).json()
    assert fresh['archived'] is False and fresh['pinned'] is False
    assert fresh['owner']['name'] == 'Maya Updated'


@pytest.mark.parametrize('admin', [True, False])
@pytest.mark.parametrize('durable_goal', [True, False])
def test_rich_conversation_metadata_reuses_one_pool_slot(login, monkeypatch, admin, durable_goal):
    app, client = login
    actor = client.get('/api/session').json()['user_id']
    store = app.state.store
    root = store.create_slack_run('EvMetadata', 'Read <@U12345678>', [], 'C12345678',
                                 '1790719000.123456', 'U87654321', team_id='T12345678', harness='hermes')
    child = store.create_run('Saved worker', '', 'demo', [], user_id=actor, harness='hermes')
    with store.connect() as conn:
        conn.execute("INSERT INTO user_roles VALUES('tin@berri.ai',?,1,'fixture',?)",
                     ('admin' if admin else 'member', now()))
        profile = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute("UPDATE users SET name='Teammate' WHERE id=?", (profile,))
        conn.execute('''INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at)
            VALUES('metadata-group',?,1,'metadata','{}','completed',?)''', (root['id'], now()))
        conn.execute("UPDATE runs SET parent_run_id=?,agent_group_id='metadata-group',status='idle' WHERE id=?",
                     (root['id'], child['id']))
        conn.execute('''INSERT INTO model_requests(id,key_hash,run_id,model,created_at,status,cost)
            VALUES('metadata-cost','',?,'model',?,'completed','1.25')''', (child['id'], now()))
        conn.execute('''INSERT INTO approvals(id,run_id,tool,arguments,status,created_at)
            VALUES('metadata-approval',?,'fixture','{"value": 1}','pending',?)''', (root['id'], now()))
    goal = {'objective': 'Retain the current goal', 'status': 'active'}
    if durable_goal:
        store.execute('INSERT INTO session_goals(run_id,state) VALUES(?,?)', (root['id'], json.dumps(goal)))
    else:
        store.event(root['id'], 'status', 'Earlier goal', {'goal_version': 1, 'phase': 'goal', 'goal': goal})
    store.artifacts.save(child['id'] + '.zip', b'worker artifact')
    if store.database:
        store.database.pool.resize(1, 1)
        store.database.pool.timeout = .5
    connect = store.connect
    depth = ContextVar('conversation_connection_depth', default=0)

    @contextmanager
    def single_slot(*args, **kwargs):
        assert depth.get() == 0, 'Conversation snapshot borrowed a nested pool connection'
        with connect(*args, **kwargs) as conn:
            token = depth.set(1)
            try:
                yield conn
            finally:
                # After-commit Slack discovery can borrow only after release.
                depth.reset(token)

    monkeypatch.setattr(store, 'connect', single_slot)
    response = client.get(f'/api/runs/{root["id"]}?activity=summary')
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail['goal'] == goal
    assert detail['approvals'][0]['arguments'] == {'value': 1}
    assert detail['messages'][0]['display_content'] == 'Read @Teammate'
    assert detail['slack_source']['channel'] == 'C12345678'
    assert detail['slack_connected'] is True
    agent = detail['agents']['groups'][0]['children'][0]
    assert agent['has_artifact'] is True and agent['id'] == child['id']
    if admin:
        assert agent['cost']['spend'] == '1.25' and detail['agents']['spend'] == '1.25'
    else:
        assert 'cost' not in agent and detail['agents']['spend'] is None
