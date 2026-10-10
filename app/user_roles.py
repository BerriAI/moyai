"""Administrator-managed Google Workspace roles, separate from spend identities."""
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import now


class RoleChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254)
    role: Literal['admin', 'member']
    revision: int = Field(ge=0)

    @field_validator('email')
    @classmethod
    def normalize_email(cls, value):
        value = value.lower()
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", value):
            raise ValueError('Enter a work email address.')
        return value


class UserRoles:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        store.execute('''CREATE TABLE IF NOT EXISTS user_roles (
            email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','member')),
            revision INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        store.execute('''CREATE TABLE IF NOT EXISTS user_role_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
            previous_role TEXT NOT NULL, role TEXT NOT NULL,
            actor TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def eligible(self, email):
        return bool(email and email.rpartition('@')[2] in self.settings.google_domains())

    def role_in(self, conn, email):
        row = conn.execute('SELECT role FROM user_roles WHERE email=?', (email,)).fetchone()
        return row['role'] if row else ('admin' if email in self.settings.google_admins() else 'member')

    def role(self, email):
        with self.store.connect() as conn:
            return self.role_in(conn, email.strip().lower())

    def admins_in(self, conn):
        admins = set(self.settings.google_admins())
        for row in conn.execute('SELECT email,role FROM user_roles'):
            if row['role'] == 'admin':
                admins.add(row['email'])
            else:
                admins.discard(row['email'])
        return {email for email in admins if self.eligible(email)}

    def directory(self):
        with self.store.connect() as conn:
            conn.begin_read()
            assignments = {row['email']: dict(row) for row in conn.execute('SELECT * FROM user_roles')}
            people = {email: {'email': email, 'name': '', 'has_signed_in': False, 'last_seen': None}
                      for email in set(self.settings.google_admins()) | assignments.keys() if self.eligible(email)}
            # Slack profiles may appear before Google sign-in. They do not grant authentication.
            for row in conn.execute("SELECT email,name,kind,updated_at FROM users WHERE kind IN ('google','cloudflare') OR (kind='slack' AND profile_eligible=1) ORDER BY updated_at"):
                email = row['email'].strip().lower()
                if not self.eligible(email):
                    continue
                person = people.setdefault(email, {'email': email, 'name': '', 'has_signed_in': False, 'last_seen': None})
                if row['kind'] in {'google', 'cloudflare'}:
                    person.update(name=row['name'], has_signed_in=True, last_seen=row['updated_at'])
                elif not person['name']:
                    person['name'] = row['name']
            for email, person in people.items():
                assignment = assignments.get(email, {})
                person.update(role=self.role_in(conn, email), revision=assignment.get('revision', 0))
            audit = [dict(row) for row in conn.execute('SELECT email,previous_role,role,actor,created_at FROM user_role_audit ORDER BY id DESC LIMIT 30')]
        return {'users': sorted(people.values(), key=lambda row: (row['role'] != 'admin', row['email'])),
                'activity': audit, 'domains': sorted(self.settings.google_domains())}

    def change(self, body, actor):
        if not actor:
            raise HTTPException(401, 'Sign in to the workspace.')
        if not self.eligible(body.email):
            raise HTTPException(422, 'Use an email in the workspace’s allowed Google domains.')
        with self.store.connect() as conn:
            # Serialize authorization, the last-admin check, and the write. Two
            # concurrent demotions must never remove both remaining admins.
            conn.begin_write()
            if actor.get('method') in {'google', 'cloudflare'}:
                actor_email = actor['identity']['email'].strip().lower()
                if not self.eligible(actor_email) or self.role_in(conn, actor_email) != 'admin':
                    raise HTTPException(403, 'An organization administrator must perform this action.')
                actor_label = actor_email
            else:
                if actor.get('role') != 'admin' or actor.get('method') not in {'local', 'password'}:
                    raise HTTPException(403, 'An organization administrator must perform this action.')
                actor_label = 'Local preview admin' if actor['method'] == 'local' else 'Shared password admin'
            previous = conn.execute('SELECT * FROM user_roles WHERE email=?', (body.email,)).fetchone()
            revision = previous['revision'] if previous else 0
            if body.revision != revision:
                raise HTTPException(409, 'This role changed since you opened it. Refresh and try again.')
            old_role = self.role_in(conn, body.email)
            if old_role == 'admin' and body.role != 'admin' and len(self.admins_in(conn)) <= 1:
                raise HTTPException(409, 'Keep at least one administrator. Promote another user first.')
            if previous and previous['role'] == body.role:
                return {'email': body.email, 'role': body.role, 'revision': revision}
            known = previous or body.email in self.settings.google_admins() or conn.execute(
                'SELECT 1 FROM users WHERE lower(email)=? LIMIT 1', (body.email,)).fetchone()
            stamp = now()
            conn.execute('''INSERT INTO user_roles VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                role=excluded.role,revision=excluded.revision,updated_by=excluded.updated_by,updated_at=excluded.updated_at''',
                         (body.email, body.role, revision + 1, actor_label, stamp))
            conn.execute('INSERT INTO user_role_audit(email,previous_role,role,actor,created_at) VALUES(?,?,?,?,?)',
                         (body.email, old_role if known else '', body.role, actor_label, stamp))
        return {'email': body.email, 'role': body.role, 'revision': revision + 1}

    def routes(self, security):
        router = APIRouter()

        @router.get('/api/admin/users')
        async def users(request: Request):
            security.require(request, admin=True)
            return self.directory()

        @router.put('/api/admin/users/role')
        async def change_role(body: RoleChange, request: Request):
            security.require(request, admin=True, mutation=True)
            return self.change(body, security.session_info(request))

        return router
