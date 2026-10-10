"""Bounded, read-only observations attached to shared session history."""
from sandbox.activity import public_text

from .agents import AgentCoordinator
from .workspace_diagnostics import failure_record


def agent_summary(conn, run_id):
    waiting = None
    if 'durable_sessions' in conn.table_names():
        row = conn.execute("SELECT json_text(state,'wait_group') AS wait_group FROM durable_sessions WHERE run_id=?", (run_id,)).fetchone()
        waiting = row['wait_group'] if row else None
    rows = conn.execute('''SELECT id,status FROM agent_groups WHERE parent_id=?
        ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT 4''',
        (run_id, waiting or '')).fetchall()
    groups = []
    for row in rows[:3]:
        children = conn.execute('''SELECT id,agent_label,status,updated_at FROM runs
            WHERE agent_group_id=? AND deleted_at='' AND deletion_requested_at=''
            ORDER BY created_at,id LIMIT 21''', (row['id'],)).fetchall()
        groups.append({'id': row['id'], 'status': row['status'],
                       'current_settled': row['status'] != 'preparing' and not bool(AgentCoordinator.unsettled_in(conn, row['id'])),
                       'children_scope': 'current', 'children_truncated': len(children) > 20,
                       'children': [{**dict(child), 'agent_label': public_text(child['agent_label'], 120)} for child in children[:20]]})
    return {'waiting_group_id': waiting if any(row['id'] == waiting for row in rows) else None,
            'groups': groups, 'groups_truncated': len(rows) > 3}


def read_diagnostics(store, run_id):
    with store.connect() as conn:
        conn.begin_read()
        last = conn.execute('''SELECT max(created_at) FROM (
            SELECT max(created_at) AS created_at FROM messages WHERE run_id=? AND status!='deleted'
            UNION ALL SELECT max(created_at) FROM events WHERE run_id=?) activity''', (run_id, run_id)).fetchone()[0]
        agents = agent_summary(conn, run_id)
        failures = conn.execute("""SELECT id,created_at,data FROM events WHERE run_id=? AND kind='error'
            AND json_text(data,'phase') IN ('broker_failure','sdk_failure') ORDER BY id DESC LIMIT 5""", (run_id,)).fetchall()
        recent_failures = [{key: public_text(value, 200) if isinstance(value, str) else value
                            for key, value in failure_record(row).items()} for row in failures]
    return last, agents, recent_failures
