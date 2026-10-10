from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace

import pytest

from app.swarm_collaboration import (
    Assign, Board, ClaimLead, Publish, SwarmCollaboration, create_team_in, initialize_schema,
)
from app.swarms import create_in
from app.db import now
from test_agents import launch
from test_durable import durable


def activate(store, member_id):
    store.claim_message(member_id)
    store.update_run(member_id, status='running')
    return store.run(member_id)


async def team(durable, count=3):
    coordinator, result, _ = await launch(durable, count=count)
    collaboration = SwarmCollaboration(coordinator)
    with coordinator.store.connect() as conn:
        conn.begin_write()
        create_team_in(conn, result['group_id'], 'Find a practical way to improve team collaboration.')
    members = [activate(coordinator.store, row['id']) for row in coordinator.children(result['group_id'])]
    return collaboration, members, result['group_id']


async def test_board_persists_goal_roster_posts_and_assignments(durable):
    collaboration, members, group_id = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='I will organize independent proposals and compare them.'))
    assignment = collaboration.assign(lead, Assign(member_id=worker['id'], task='Find a useful proposal.'))
    collaboration.publish(worker, Publish(message='A shared task board makes ownership explicit.'))
    replacement = SwarmCollaboration(collaboration.coordinator)
    board = replacement.board(worker)
    assert board['group_id'] == group_id
    assert board['task'] == 'Find a practical way to improve team collaboration.'
    assert board['leader_id'] == lead['id']
    assert {member['id'] for member in board['members']} == {member['id'] for member in members}
    assert all(member['harness'] == 'hermes' and member['model'] == 'test-model' for member in board['members'])
    assert [post['kind'] for post in board['posts']] == ['leadership', 'assignment', 'message']
    assert board['assignments'][0]['id'] == assignment['post_id']
    messages = collaboration.store.messages(worker['id'])
    assert 'untrusted teammate' in messages[-1]['content']
    assert 'Find a useful proposal.' in messages[-1]['content']
    event = collaboration.store.events(lead['parent_run_id'])[-1]
    assert event['data']['post_id'] == board['posts'][-1]['id']
    parent = collaboration.store.run(lead['parent_run_id'])
    assert worker['active_message_id'] != parent['active_message_id']
    assert event['data']['turn_id'] == parent['active_message_id']
    assert event['data']['member_turn_id'] == worker['active_message_id']


async def test_first_willing_lead_wins_concurrent_claims_and_retries(durable):
    collaboration, members, group_id = await team(durable)
    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        futures = [pool.submit(collaboration.claim_lead, member, ClaimLead(plan='I can coordinate.')) for member in members]
        claims = [future.result() for future in futures]
    winners = [claim for claim in claims if claim['is_lead']]
    assert len(winners) == 1
    assert {claim['leader_id'] for claim in claims} == {winners[0]['leader_id']}
    lead = next(member for member in members if member['id'] == winners[0]['leader_id'])
    assert collaboration.claim_lead(lead, ClaimLead(plan='I can coordinate.'))['is_lead']
    assert len(collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=?', (group_id,))) == 1


async def test_only_lead_can_assign_and_only_to_same_team(durable):
    collaboration, members, _ = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Compare proposals.'))
    with pytest.raises(ValueError, match='Only the elected'):
        collaboration.assign(worker, Assign(member_id=lead['id'], task='Take over this work.'))
    with pytest.raises(ValueError, match='member of this swarm'):
        collaboration.assign(lead, Assign(member_id=lead['parent_run_id'], task='Give the parent orders.'))
    with pytest.raises(ValueError, match='member of this swarm'):
        collaboration.assign(lead, Assign(member_id='f' * 32, task='Find a nonexistent teammate.'))


async def test_plain_delegation_and_spoofed_run_cannot_access_shared_board(durable):
    coordinator, result, _ = await launch(durable, count=1)
    collaboration = SwarmCollaboration(coordinator)
    member = activate(coordinator.store, coordinator.children(result['group_id'])[0]['id'])
    with pytest.raises(ValueError, match='shared-goal swarm'):
        collaboration.publish(member, Publish(message='Should not enter any team.'))
    with coordinator.store.connect() as conn:
        conn.begin_write()
        create_team_in(conn, result['group_id'], 'A goal for this real team.')
    parent = coordinator.store.run(member['parent_run_id'])
    spoofed = dict(parent, agent_group_id=result['group_id'], parent_run_id=parent['id'])
    with pytest.raises(ValueError, match='shared-goal swarm'):
        collaboration.board(spoofed)


@pytest.mark.parametrize('state,expired', [('paused', False), ('stopped', False), ('active', True)])
async def test_paused_stopped_and_expired_missions_reject_all_writes(durable, state, expired):
    collaboration, members, _ = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Compare proposals.'))
    with collaboration.store.connect() as conn:
        conn.begin_write()
        create_in(conn, lead['parent_run_id'], 300, '2000-01-01T00:00:00+00:00' if expired else now())
        conn.execute('UPDATE swarm_missions SET status=? WHERE run_id=?', (state, lead['parent_run_id']))
    for action in (
        lambda: collaboration.claim_lead(worker, ClaimLead(plan='I can lead.')),
        lambda: collaboration.publish(worker, Publish(message='New finding.')),
        lambda: collaboration.assign(lead, Assign(member_id=worker['id'], task='New assignment.')),
    ):
        with pytest.raises(ValueError, match='swarm is not active'):
            action()


async def test_stopping_ancestor_rejects_peer_writes(durable):
    collaboration, members, _ = await team(durable)
    collaboration.store.update_run(members[0]['parent_run_id'], status='stopping')
    with pytest.raises(ValueError, match='ancestor session'):
        collaboration.publish(members[0], Publish(message='Must not escape cancellation.'))


async def test_many_posts_coalesce_one_queued_leader_wake_and_durable_dispatch(durable):
    collaboration, members, _ = await team(durable, count=10)
    lead = members[0]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    before = collaboration.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (lead['id'],))[0]['revision']
    for index, member in enumerate(members[1:]):
        collaboration.publish(member, Publish(message=f'Independent proposal {index}.'))
    collaboration.publish(members[1], Publish(message='A tenth useful finding.'))
    pending = collaboration.store.rows("SELECT * FROM messages WHERE run_id=? AND status='queued'", (lead['id'],))
    assert len(pending) == 1 and pending[0]['client_id'].startswith('swarm:board:')
    after = collaboration.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (lead['id'],))[0]['revision']
    assert after == before + 1
    # Once a wake is being processed, one later wake preserves posts that arrive
    # after the lead read its board. Further posts still coalesce into that wake.
    collaboration.store.execute("UPDATE messages SET status='running' WHERE id=?", (pending[0]['id'],))
    collaboration.publish(members[2], Publish(message='Arrived while the lead is synthesizing.'))
    collaboration.publish(members[3], Publish(message='One more finding during synthesis.'))
    assert len(collaboration.store.rows("SELECT id FROM messages WHERE run_id=? AND status='queued'", (lead['id'],))) == 1


async def test_finished_leader_wakes_and_handoff_waits_for_board_notification(durable):
    collaboration, members, group_id = await team(durable)
    lead, publisher = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    for member in members:
        if member['id'] != publisher['id']:
            collaboration.store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (member['id'],))
            collaboration.store.update_run(member['id'], status='idle')
    result = collaboration.publish(publisher, Publish(message='Final peer evidence for the lead.'))
    collaboration.store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (publisher['id'],))
    collaboration.store.update_run(publisher['id'], status='idle')
    assert collaboration.store.run(lead['id'])['status'] == 'queued'
    assert not collaboration.coordinator.handoff(lead['parent_run_id'], group_id)
    collaboration.store.execute("UPDATE messages SET status='completed' WHERE id=?", (result['wake_message_id'],))
    collaboration.store.update_run(lead['id'], status='idle')
    assert collaboration.coordinator.handoff(lead['parent_run_id'], group_id)


async def test_repeated_posts_and_assignments_do_not_duplicate_messages_or_wakes(durable):
    collaboration, members, group_id = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    assignment = Assign(member_id=worker['id'], task='Analyze a useful alternative.')
    first = collaboration.assign(lead, assignment)
    collaboration.store.execute("UPDATE runs SET model='openai/gpt-6.1-sol',active_model='openai/gpt-6.1-sol' WHERE id=?", (worker['id'],))
    second = collaboration.assign(lead, assignment)
    assert first['message_id'] == second['message_id']
    assert first['created'] and not second['created']
    post = Publish(message='A useful alternative has lower coordination overhead.')
    first = collaboration.publish(worker, post)
    second = collaboration.publish(worker, post)
    assert first['post_id'] == second['post_id']
    assert first['created'] and not second['created']
    assert len(collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=?', (group_id,))) == 3
    assert len(collaboration.store.messages(worker['id'])) == 2
    assert collaboration.store.messages(worker['id'])[-1]['model'] == 'test-model'
    assert len(collaboration.store.messages(lead['id'])) == 2


async def test_failed_leader_is_not_automatically_retried_by_peer_post(durable):
    collaboration, members, _ = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    collaboration.store.execute("UPDATE messages SET status='failed' WHERE run_id=?", (lead['id'],))
    collaboration.store.update_run(lead['id'], status='failed')
    result = collaboration.publish(worker, Publish(message='This finding remains on the board.'))
    assert result['created'] and result['wake_message_id'] is None
    board = collaboration.board(worker)
    assert next(member['status'] for member in board['members'] if member['id'] == lead['id']) == 'failed'
    assert collaboration.store.run(lead['id'])['status'] == 'failed'


@pytest.mark.parametrize('status,deleted_field', [
    ('failed', ''), ('cancelled', ''), ('interrupted', ''), ('stopping', ''),
    ('idle', 'deleted_at'), ('idle', 'deletion_requested_at'),
])
async def test_lead_cannot_revive_failed_stopped_or_deleted_member(durable, status, deleted_field):
    collaboration, members, group_id = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    collaboration.store.execute('UPDATE runs SET status=? WHERE id=?', (status, worker['id']))
    collaboration.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=?", (worker['id'],))
    if deleted_field:
        collaboration.store.execute(f'UPDATE runs SET {deleted_field}=? WHERE id=?', (now(), worker['id']))
    member_before = collaboration.store.run(worker['id'])
    messages_before = collaboration.store.messages(worker['id'])
    journal_before = collaboration.store.rows('SELECT * FROM durable_sessions WHERE run_id=?', (worker['id'],))
    posts_before = collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=?', (group_id,))
    with pytest.raises(ValueError, match='explicit owner recovery'):
        collaboration.assign(lead, Assign(member_id=worker['id'], task='Continue the interrupted work.'))
    assert collaboration.store.run(worker['id']) == member_before
    assert collaboration.store.messages(worker['id']) == messages_before
    assert collaboration.store.rows('SELECT * FROM durable_sessions WHERE run_id=?', (worker['id'],)) == journal_before
    assert collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=?', (group_id,)) == posts_before


async def test_queue_failure_rolls_back_post_trace_and_assignment(durable, monkeypatch):
    collaboration, members, group_id = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Collect proposals.'))
    for index in range(5):
        collaboration.coordinator.enqueue_child(worker['id'], f'Pending human direction {index}',
            f'human-{index}', worker['model'], worker['owner_id'])
    events_before = len(collaboration.store.events(lead['parent_run_id']))
    def unavailable(*args, **kwargs):
        raise ValueError('Queue persistence unavailable')
    monkeypatch.setattr(collaboration.coordinator, 'enqueue_child_in', unavailable)
    with pytest.raises(ValueError, match='Queue persistence unavailable'):
        collaboration.assign(lead, Assign(member_id=worker['id'], task='Must not partially save.'))
    assert len(collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=?', (group_id,))) == 1
    assert len(collaboration.store.events(lead['parent_run_id'])) == events_before


async def test_shared_goal_is_immutable_and_replayed_registration_is_safe(durable):
    collaboration, _, group_id = await team(durable)
    with collaboration.store.connect() as conn:
        conn.begin_write()
        create_team_in(conn, group_id, 'Find a practical way to improve team collaboration.')
    with pytest.raises(ValueError, match='different shared goal'):
        with collaboration.store.connect() as conn:
            conn.begin_write()
            create_team_in(conn, group_id, 'Sneak in a different goal.')


async def test_board_pagination_preserves_every_post_and_incremental_updates(durable):
    collaboration, members, group_id = await team(durable)
    lead, worker = members[:2]
    collaboration.claim_lead(lead, ClaimLead(plan='Read every teammate finding before synthesizing.'))
    for index in range(105):
        collaboration.publish(worker, Publish(message=f'Independent finding {index}.'))
    cursor = 0
    pages = []
    while True:
        page = collaboration.board(lead, Board(after_id=cursor))
        assert page['group_id'] == group_id
        assert page['task'] == 'Find a practical way to improve team collaboration.'
        assert len(page['members']) == 3
        assert len(page['posts']) <= 50
        pages.append(page)
        cursor = page['next_after_id']
        if not page['has_more']:
            break
    posts = [post for page in pages for post in page['posts']]
    expected = collaboration.store.rows('SELECT * FROM swarm_posts WHERE group_id=? ORDER BY id', (group_id,))
    assert [len(page['posts']) for page in pages] == [50, 50, 6]
    assert [post['id'] for post in posts] == [post['id'] for post in expected]
    assert len({post['id'] for post in posts}) == 106
    empty = collaboration.board(lead, Board(after_id=cursor))
    assert empty['posts'] == [] and empty['next_after_id'] == cursor and not empty['has_more']
    update = collaboration.publish(worker, Publish(message='Finding after the lead finished reading.'))
    next_page = collaboration.board(lead, Board(after_id=cursor))
    assert [post['id'] for post in next_page['posts']] == [update['post_id']]
    assert next_page['next_after_id'] == update['post_id'] and not next_page['has_more']
    assert len(collaboration.store.rows("SELECT id FROM messages WHERE run_id=? AND status='queued' AND client_id LIKE 'swarm:board:%'", (lead['id'],))) == 1


def test_member_deletion_preserves_historical_posts_without_blocking_deletion():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('CREATE TABLE agent_groups(id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE runs(id TEXT PRIMARY KEY)')
        initialize_schema(SimpleNamespace(execute=conn.execute))
        conn.execute("INSERT INTO agent_groups VALUES('group')")
        conn.execute("INSERT INTO runs VALUES('member')")
        create_team_in(conn, 'group', 'Preserve public work history.')
        conn.execute('''INSERT INTO swarm_posts(group_id,member_id,kind,content,dedupe_key,created_at)
            VALUES('group','member','message','Useful evidence','once','now')''')
        conn.execute("DELETE FROM runs WHERE id='member'")
        assert conn.execute('SELECT content FROM swarm_posts').fetchone()['content'] == 'Useful evidence'
        conn.execute("DELETE FROM agent_groups WHERE id='group'")
        assert not conn.execute('SELECT * FROM swarm_posts').fetchall()
