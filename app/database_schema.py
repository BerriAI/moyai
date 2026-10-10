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
    OR json_type(state, '$.phase') = 'null')"""
POSTGRES_OCCUPIED_SESSION = """((state::jsonb ->> 'phase')
    NOT IN ('idle', 'waiting_children', 'waiting_credential', 'waiting_environment')
    OR jsonb_typeof(state::jsonb -> 'phase') = 'null')"""
SQLITE_OCCUPIED_INDEX = f'CREATE INDEX idx_durable_sessions_occupied ON durable_sessions(run_id) WHERE {SQLITE_OCCUPIED_SESSION}'
POSTGRES_OCCUPIED_INDEX = f'CREATE INDEX idx_durable_sessions_occupied ON durable_sessions(run_id) WHERE {POSTGRES_OCCUPIED_SESSION}'
