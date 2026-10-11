"""Explicit backend definitions shared by runtime startup and offline import."""

# An explicit port of the permission-revocation trigger, guarded against drift.
# SQLite removes IF NOT EXISTS when it saves this definition in sqlite_schema.
REVOKE_TRIGGER = '''CREATE TRIGGER revoke_github_write_access
    AFTER UPDATE OF status,deleted_at ON runs
    WHEN NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!=''
    BEGIN UPDATE github_write_access SET status='revoked'
    WHERE run_id=NEW.id AND status IN ('pending','approved'); END'''
REVOKE_FUNCTION = '''CREATE FUNCTION revoke_github_write_access_fn() RETURNS trigger
    LANGUAGE plpgsql SET search_path FROM CURRENT AS $moyai$
    BEGIN
        UPDATE github_write_access SET status='revoked'
        WHERE run_id=NEW.id AND status IN ('pending','approved');
        RETURN NEW;
    END
    $moyai$'''
REVOKE_POSTGRES = '''CREATE TRIGGER revoke_github_write_access
    AFTER UPDATE OF status,deleted_at ON runs FOR EACH ROW
    WHEN (NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!='')
    EXECUTE FUNCTION revoke_github_write_access_fn()'''


SQLITE_OCCUPIED_SESSION = """(json_extract(state, '$.phase')
    NOT IN ('idle', 'waiting_children', 'waiting_credential', 'waiting_environment')
    OR json_type(state, '$.phase') = 'null')
    AND (json_extract(state, '$.independent_runtime') IS NOT 1
         OR coalesce(json_extract(state, '$.computer.phase'), 'idle') NOT IN ('idle','waiting_environment'))"""
POSTGRES_OCCUPIED_SESSION = """((state::jsonb ->> 'phase')
    NOT IN ('idle', 'waiting_children', 'waiting_credential', 'waiting_environment')
    OR jsonb_typeof(state::jsonb -> 'phase') = 'null')
    AND ((state::jsonb ->> 'independent_runtime') IS DISTINCT FROM 'true'
         OR coalesce(state::jsonb #>> '{computer,phase}', 'idle') NOT IN ('idle','waiting_environment'))"""
SQLITE_OCCUPIED_INDEX = f'CREATE INDEX idx_durable_sessions_occupied ON durable_sessions(run_id) WHERE {SQLITE_OCCUPIED_SESSION}'
POSTGRES_OCCUPIED_INDEX = f'CREATE INDEX idx_durable_sessions_occupied ON durable_sessions(run_id) WHERE {POSTGRES_OCCUPIED_SESSION}'


# Transactional, channel-independent change capture. Inboxes coalesce by
# consumer/session, so idle history is never revisited and memory is bounded.
SESSION_EVENT_FUNCTION = '''CREATE FUNCTION session_event_changed_fn() RETURNS trigger
    LANGUAGE plpgsql SET search_path FROM CURRENT AS $moyai$
    DECLARE session_id text;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            session_id := to_jsonb(OLD) ->> TG_ARGV[0];
        ELSE
            session_id := to_jsonb(NEW) ->> TG_ARGV[0];
        END IF;
        INSERT INTO session_event_inbox(consumer,run_id,kind)
            SELECT name,session_id,TG_ARGV[1] FROM session_event_consumers WHERE true
            ON CONFLICT(consumer,run_id) DO UPDATE SET kind=excluded.kind;
        PERFORM pg_notify('moyai_session_events_' || md5(TG_TABLE_SCHEMA), '');
        RETURN NULL;
    END
    $moyai$'''

SESSION_EVENT_TRIGGERS = {}
for _table, _field, _columns, _kind in (
    ('runs', 'id', ('status','parent_run_id','deleted_at','deletion_requested_at','owner_id'), 'session.state_changed'),
    ('messages', 'run_id', ('status',), 'session.reply_changed'),
    ('approvals', 'run_id', ('status',), 'session.approval_changed'),
    ('credential_requests', 'run_id', ('status','generation'), 'session.access_changed'),
):
    for _action in ('INSERT', 'UPDATE', 'DELETE'):
        if _table == 'runs' and _action == 'DELETE':
            continue
        _name = f'session_event_{_table}_{_action.lower()}'
        _verb = _action + (' OF ' + ','.join(_columns) if _action == 'UPDATE' else '')
        _row = 'OLD' if _action == 'DELETE' else 'NEW'
        _changed = ' OR '.join(f'OLD.{column} IS NOT NEW.{column}' for column in _columns)
        _when = ' WHEN ' + _changed if _action == 'UPDATE' else ''
        _sqlite = f'''CREATE TRIGGER {_name} AFTER {_verb} ON {_table}{_when}
            BEGIN INSERT INTO session_event_inbox(consumer,run_id,kind)
                SELECT name,{_row}.{_field},'{_kind}' FROM session_event_consumers WHERE 1
                ON CONFLICT(consumer,run_id) DO UPDATE SET kind=excluded.kind; END'''
        _pg_when = ' WHEN (' + _changed.replace(' IS NOT ', ' IS DISTINCT FROM ') + ')' if _action == 'UPDATE' else ''
        _postgres = f'''CREATE TRIGGER {_name} AFTER {_verb} ON {_table}
            FOR EACH ROW{_pg_when} EXECUTE FUNCTION session_event_changed_fn('{_field}','{_kind}')'''
        SESSION_EVENT_TRIGGERS[_name] = (_sqlite, _postgres)
