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
    if not run_id:
        return False
    rows = store.rows('SELECT private_owner_id FROM runs WHERE id=?', (run_id,), connection=connection)
    return bool(rows and rows[0]['private_owner_id'])


def require_owner(store, run_id, actor='', connection=None):
    run = stored_run(store, run_id, connection)
    if not SessionPrivacy.can_access(run, actor) or run.get('deleted_at'):
        raise HTTPException(404, 'Session not found.')
    return run


def require_request_owner(store, security, request, run_id):
    security.require(request)
    info = security.session_info(request)
    actor = getattr(request.state, 'access_actor', None) if info and info.get('method') == 'cloudflare' else None
    if actor is None:
        actor = store.identity(info)
    require_owner(store, run_id, actor)
    return actor


def deny_export(store, run_id, connection=None):
    if private_run(store, run_id, connection):
        raise HTTPException(403, 'Export from private sessions is not supported.')
