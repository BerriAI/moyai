"""Small sink-local guards; the HTTP privacy service owns session admission.

Never infer authorization from admin role, mutable requester or caller-supplied
privacy metadata. Re-read the persisted run at each content release boundary.
"""
from fastapi import HTTPException

from .session_privacy import SessionPrivacy


def stored_run(store, run_id, connection=None):
    if connection is not None:
        row = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None
    return store.run(run_id)


def private_run(store, run_id, connection=None):
    run = stored_run(store, run_id, connection)
    return SessionPrivacy.is_private(run)


def require_owner(store, run_id, actor='', connection=None):
    run = stored_run(store, run_id, connection)
    if not SessionPrivacy.can_access(run, actor) or run.get('deleted_at'):
        raise HTTPException(404, 'Session not found.')
    return run


def require_request_owner(store, security, request, run_id):
    security.require(request)
    actor = store.identity(security.session_info(request))
    require_owner(store, run_id, actor)
    return actor


def deny_export(store, run_id, connection=None):
    if private_run(store, run_id, connection):
        raise HTTPException(403, 'Export from private sessions is not supported.')
