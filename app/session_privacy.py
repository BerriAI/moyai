"""One authorization boundary for immutable, individually owned sessions."""
from fastapi import HTTPException


class SessionPrivacy:
    def __init__(self, store, security=None):
        self.store, self.security = store, security

    @staticmethod
    def is_private(run):
        return bool(run and 'private_owner_id' in run.keys() and run['private_owner_id'])

    @staticmethod
    def can_access(run, actor_id):
        return bool(run) and (not SessionPrivacy.is_private(run) or run['private_owner_id'] == actor_id)

    def require_owner(self, run, actor_id):
        if not self.can_access(run, actor_id):
            raise HTTPException(404, 'Session not found.')
        return run

    def individual_owner(self, request):
        info = self.security.session_info(request)
        if not info or info.get('method') not in {'google', 'cloudflare'}:
            raise HTTPException(403, 'Private sessions require individual Google or Cloudflare sign-in.')
        return self.store.identity(info)

    def require_request(self, request, run_or_id):
        info = self.security.session_info(request)
        if not info:
            raise HTTPException(401, 'Sign in to the workspace.')
        run = self.store.run(run_or_id) if isinstance(run_or_id, str) else run_or_id
        actor = self.individual_owner(request) if self.is_private(run) and info.get('method') in {'google', 'cloudflare'} else ''
        return self.require_owner(run, actor)

    @staticmethod
    def require_no_export(run):
        if SessionPrivacy.is_private(run):
            raise HTTPException(403, 'Private sessions cannot export, share, automate, or delegate content.')

    @staticmethod
    def allow_tool(run, name, write=False):
        if not SessionPrivacy.is_private(run):
            return True
        return not (write or name.startswith(('automation_', 'agents_'))
                    or name in {'media_share', 'media_revoke', 'skills_save', 'memory_save', 'memory_forget'}
                    or (name.startswith('credentials_') and name not in {'credentials_list', 'credentials_http_request'}))

    def guard_request(self, request):
        """Resolve indirect resource routes before their handlers can read content."""
        if not getattr(request, 'scope', {}).get('path', '').startswith('/api/'):
            return
        params = request.path_params
        run_id = params.get('run_id') or request.query_params.get('run_id')
        if not run_id and params.get('attachment_id'):
            rows = self.store.rows('SELECT m.run_id FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.id=?',
                                   (params['attachment_id'],))
            run_id = rows[0]['run_id'] if rows else None
        if not run_id and params.get('approval_id'):
            rows = self.store.rows('SELECT run_id FROM approvals WHERE id=?', (params['approval_id'],))
            run_id = rows[0]['run_id'] if rows else None
        if run_id:
            self.require_request(request, run_id)
