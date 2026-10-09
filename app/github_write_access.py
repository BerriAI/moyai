"""Authenticated, exact-chat consent; never publication provenance."""
import hashlib
import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from .connector_errors import ConnectorError
from .db import now


class WriteDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'deny', 'revoke']


class GitHubWriteAccess:
    def init_write_access(self):
        self.store.execute('''CREATE TABLE IF NOT EXISTS github_write_access (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            connection_version TEXT NOT NULL, repository_id INTEGER NOT NULL,
            number INTEGER NOT NULL, head_repository_id INTEGER NOT NULL,
            branch TEXT NOT NULL, base_branch TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','revoked')),
            created_at TEXT NOT NULL)''')
        self.store.execute('''CREATE INDEX IF NOT EXISTS github_write_access_scope
            ON github_write_access(run_id,connection_version,repository_id,number)''')
        if self.store.database:
            from .postgres_migration import REVOKE_FUNCTION, REVOKE_POSTGRES
            with self.store.connect() as conn:
                conn.begin_write()
                conn.execute(REVOKE_FUNCTION.replace('CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION'))
                conn.execute(REVOKE_POSTGRES.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER'))
        else:
            self.store.execute('''CREATE TRIGGER IF NOT EXISTS revoke_github_write_access
                AFTER UPDATE OF status,deleted_at ON runs
                WHEN NEW.status IN ('stopping','cancelled') OR NEW.deleted_at!=''
                BEGIN UPDATE github_write_access SET status='revoked'
                WHERE run_id=NEW.id AND status IN ('pending','approved'); END''')

    def write_actor(self, run, version, tool):
        self.ensure_publish_allowed(run, version, tool)
        current = self.store.run(run['id'])
        if (current['deleted_at'] or not current['chat_enabled'] or not current['active_message_id'] or current['parent_run_id']
                or not current['active_user_id'] or current['active_user_id'] != run.get('active_user_id')
                or self.store.rows('SELECT 1 FROM automation_runs WHERE run_id=?', (run['id'],))):
            raise ConnectorError('PR write approval requires this direct chat’s active requester.')
        return current['active_user_id']

    def same_write_requester(self, left, right):
        if left == right:
            return bool(left)
        match = getattr(self.connectors, 'github_requester_match', None)
        return bool(match and (match(left, right) or match(right, left)))

    def find_access(self, run, actor, version, target, number):
        rows = self.store.rows('''SELECT * FROM github_write_access WHERE run_id=?
            AND connection_version=? AND repository_id=? AND number=? ORDER BY created_at,id''',
            (run['id'], version, target, number))
        return self.effective_access(rows, actor)

    def effective_access(self, rows, actor):
        # Identity verification can link previously independent requests later.
        # A terminal decision must win; a pending alias must not shadow consent.
        priority = {'revoked': 0, 'denied': 1, 'approved': 2, 'pending': 3}
        matches = [row for row in rows if self.same_write_requester(row['actor_id'], actor)]
        return min(matches, key=lambda row: priority[row['status']], default=None)

    def access_id(self, run, actor, version, target, number):
        return hashlib.sha256(json.dumps([run['id'], actor, version, target, number]).encode()).hexdigest()

    @staticmethod
    def valid_branch(value):
        return (isinstance(value, str) and 0 < len(value) <= 1024
                and value != '@' and not value.endswith('.')
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '~^:?*[\\' for c in value)
                and '..' not in value and '@{' not in value
                and all(part and not part.startswith('.') and not part.endswith('.lock') for part in value.split('/')))

    @staticmethod
    def pr_identity(pr, target, number):
        if not isinstance(pr, dict):
            raise ConnectorError('GitHub returned invalid PR metadata.')
        head, base = pr.get('head') or {}, pr.get('base') or {}
        if (not isinstance(head, dict) or not isinstance(base, dict)
                or not isinstance(head.get('repo'), dict) or not isinstance(base.get('repo'), dict)):
            raise ConnectorError('GitHub did not confirm the PR repositories.')
        repo = (head.get('repo') or {}).get('id')
        branch, base_branch = head.get('ref', ''), base.get('ref', '')
        if (pr.get('number') != number or (base.get('repo') or {}).get('id') != target
                or pr.get('state') != 'open' or pr.get('merged') or type(repo) is not int or repo <= 0
                or not GitHubWriteAccess.valid_branch(branch) or not GitHubWriteAccess.valid_branch(base_branch)
                or not isinstance(head.get('sha'), str) or not re.fullmatch(r'[0-9a-f]{40}', head['sha'])):
            raise ConnectorError('GitHub did not confirm an open PR and its exact base/head identity.')
        return repo, branch, base_branch

    async def request_write_access(self, run, args):
        target = await self.selected_target(run, args.repository, args.repository_id)
        version = run.get('github_connection_version') or self.connection_version()
        actor = self.write_actor(run, version, 'github_request_pull_request_write_access')
        token = await self.installation_token(repository=target, write=True)
        try:
            publication = self.owned_publication(target, args.number, version)
        except ConnectorError:
            publication = None
        if publication:
            await self.owned_pr(token, target, args.number, publication)
            self.write_actor(run, version, 'github_request_pull_request_write_access')
            return {**self.receipt(publication['result'], target), 'status': 'publication_access',
                    'instruction': 'This PR already has confirmed workspace publication access. No additional chat approval is needed.'}
        pr = await self.request('GET', f'/repositories/{target}/pulls/{args.number}', token=token)
        head, branch, base = self.pr_identity(pr, target, args.number)
        self.write_actor(run, version, 'github_request_pull_request_write_access')
        existing = self.find_access(run, actor, version, target, args.number)
        identity = existing['id'] if existing else self.access_id(run, actor, version, target, args.number)
        self.store.execute('''INSERT INTO github_write_access
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING''', (identity, run['id'], actor, version, target, args.number,
            head, branch, base, str(pr.get('title', ''))[:500], 'pending', now()))
        row = self.store.rows('SELECT * FROM github_write_access WHERE id=?', (identity,))[0]
        if existing is None:
            self.store.event(run['id'], 'approval', f'PR write access requested: {self.repository_name(target)} #{args.number}')
        if (row['head_repository_id'], row['branch'], row['base_branch']) != (head, branch, base):
            raise ConnectorError('The PR identity changed; the existing approval cannot be reused.')
        return {**self.public_access(row), 'url': self.settings.public_url.rstrip('/') + '/#run=' + run['id'],
                'instruction': 'Only the requester can decide in the chat PR access panel. Never operate that panel yourself. If pending, finish this response and wait for their follow-up. Repeat this tool to check status; denied/revoked access cannot be regranted.'}

    def public_access(self, row):
        return {key: row[key] for key in ('id', 'repository_id', 'number', 'head_repository_id', 'branch', 'base_branch', 'title', 'status')} | {'repository': self.repository_name(row['repository_id'])}

    def access_requests(self, run, actor):
        if not run or run['deleted_at'] or not self.same_write_requester(run['active_user_id'], actor):
            return []
        rows = self.store.rows('SELECT * FROM github_write_access WHERE run_id=? AND connection_version=? ORDER BY created_at',
                               (run['id'], self.connection_version()))
        return [self.public_access(r) for r in rows if self.same_write_requester(r['actor_id'], actor)]

    async def decide_write_access(self, run_id, identity, actor, decision):
        with self.store.connect() as conn:
            conn.begin_write()
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            row = conn.execute('SELECT * FROM github_write_access WHERE id=? AND run_id=?', (identity, run_id)).fetchone()
            if not row or not run or not self.same_write_requester(row['actor_id'], actor) or not self.same_write_requester(run['active_user_id'], actor):
                raise HTTPException(403, 'Only this request’s active requester can decide.')
            if decision == 'approve' and (run['deleted_at'] or run['status'] in {'stopping', 'cancelled'} or run['parent_run_id']
                    or row['connection_version'] != self.connection_version()
                    or not self.connectors.allowed('github_request_pull_request_write_access')):
                raise HTTPException(409, 'The chat or GitHub connection no longer permits this approval.')
            aliases = conn.execute('''SELECT * FROM github_write_access WHERE run_id=?
                AND connection_version=? AND repository_id=? AND number=?''',
                (run_id, row['connection_version'], row['repository_id'], row['number'])).fetchall()
            aliases = [item for item in aliases if self.same_write_requester(item['actor_id'], actor)]
            effective = self.effective_access(aliases, actor)
            desired = {'approve': 'approved', 'deny': 'denied', 'revoke': 'revoked'}[decision]
            if desired == 'approved' and effective['status'] in {'denied', 'revoked'}:
                raise HTTPException(409, 'This decision is final; access cannot be regranted.')
            if row['status'] != desired:
                if not ((row['status'] == 'pending' and desired in {'approved', 'denied'})
                        or (row['status'] == 'approved' and desired == 'revoked')):
                    raise HTTPException(409, 'This decision is final; access cannot be regranted.')
                conn.execute('UPDATE github_write_access SET status=? WHERE id=?', (desired, identity))
            if desired in {'denied', 'revoked'}:
                # Persist the decision on all currently verified aliases so a
                # later stale profile cannot resurrect an older approval.
                conn.executemany('UPDATE github_write_access SET status=? WHERE id=?',
                                 [(desired, item['id']) for item in aliases])
        if row['status'] != desired:
            self.store.event(run_id, 'approval', f"PR #{row['number']} write access: {desired}")
        return {'status': desired}

    def grant(self, run, target, number, version, tool):
        actor = self.write_actor(run, version, tool)
        row = self.find_access(run, actor, version, target, number)
        if not row or row['status'] != 'approved':
            raise ConnectorError('This PR needs explicit requester approval for this chat. Use github_request_pull_request_write_access; only the requester can grant it in the UI.')
        return row

    def write_authority(self, run, target, number, version, tool):
        try:
            return self.owned_publication(target, number, version)
        except ConnectorError:
            row = self.grant(run, target, number, version, tool)
            return {**row, 'grant': True, 'result': json.dumps({'repository_id': target,
                'repository': self.repository_name(target), 'number': number, 'branch': row['branch'],
                'url': f'https://github.com/{self.repository_name(target)}/pull/{number}'})}

    async def authorized_pr(self, run, token, target, number, authority, version, tool):
        if not authority.get('grant'):
            return await self.owned_pr(token, target, number, authority)
        self.grant(run, target, number, version, tool)
        pr = await self.request('GET', f'/repositories/{target}/pulls/{number}', token=token)
        identity = self.pr_identity(pr, target, number)
        if identity != (authority['head_repository_id'], authority['branch'], authority['base_branch']):
            raise ConnectorError('The PR was retargeted or its head changed identity; approval no longer applies.')
        self.grant(run, target, number, version, tool)
        return pr
