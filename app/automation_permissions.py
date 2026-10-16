"""Owner-granted instruction/schedule edits, using existing automation receipts.

Saved self-management applies only to an automation and its descendants. A chat
grant applies only while its authorizing human turn remains current. Neither
capability changes connections, event sources, ownership, budgets, or recipients.
"""
import json
from typing import Literal

from fastapi import HTTPException

from .automation_definitions import SelfManagement
from .db import now

Permission = Literal['instructions', 'schedule']


class AutomationPermissions:
    def __init__(self, store):
        self.store = store
        if store.schema_updates:
            initialize_schema(store)

    @staticmethod
    def targets(conn, run, actor):
        result = {}
        for row in conn.execute('''SELECT a.id,a.owner_id,a.definition FROM run_ancestry tree
                JOIN automation_runs occurrence ON occurrence.run_id=tree.ancestor_id
                JOIN automations a ON a.id=occurrence.automation_id
                WHERE tree.run_id=? AND a.owner_id=?''', (run['id'], actor)):
            allowed = SelfManagement.model_validate(json.loads(row['definition']).get('self_management', {})).allowed()
            if allowed:
                result[row['id']] = set(allowed)
        # A saved grant is provenance, not a bearer token. Recheck its source
        # turn, current requester and live ancestry whenever it is exercised.
        for row in conn.execute('''SELECT g.automation_id,g.permissions,r.active_user_id FROM automation_grants g
                JOIN run_ancestry tree ON tree.ancestor_id=g.run_id
                JOIN runs r ON r.id=g.run_id
                JOIN messages m ON m.id=g.message_id AND m.run_id=g.run_id
                JOIN automations a ON a.id=g.automation_id
                WHERE tree.run_id=? AND g.owner_id=? AND a.owner_id=g.owner_id
                AND r.active_message_id=g.message_id AND r.active_user_id=m.user_id
                AND r.deleted_at='' AND r.deletion_requested_at=''
                AND r.status IN ('running','reconnecting','awaiting_approval','waiting_children')
                AND m.role='user' AND m.input_kind='user' AND m.status!='deleted'
                AND COALESCE(m.client_id,'') NOT LIKE 'swarm:%' ''', (run['id'], actor)):
            result.setdefault(row['automation_id'], set()).update(json.loads(row['permissions']))
        return {key: value for key, value in result.items() if value}

    def require(self, conn, run, actor, automation_id='', permissions=()):
        targets = self.targets(conn, run, actor)
        if not automation_id:
            if len(targets) != 1:
                raise HTTPException(422, 'Select an automation_id from the granted automation targets.')
            automation_id = next(iter(targets))
        allowed = targets.get(automation_id, set())
        if not allowed or not set(permissions) <= allowed:
            raise HTTPException(403, 'The owner has not granted this automation change for the current execution.')
        return automation_id, sorted(allowed)

    def human_source(self, conn, run):
        source = conn.execute('''SELECT 1 FROM messages WHERE run_id=? AND id=? AND user_id=?
            AND role='user' AND input_kind='user' AND status!='deleted'
            AND COALESCE(client_id,'') NOT LIKE 'swarm:%' ''',
            (run['id'], run['active_message_id'], run['active_user_id'])).fetchone()
        if not source or run['parent_run_id'] or conn.execute('SELECT 1 FROM automation_runs WHERE run_id=?', (run['id'],)).fetchone():
            raise HTTPException(403, 'Only an authenticated human turn in a direct chat can grant automation management.')

    def grant(self, conn, run, actor, automation_id, permissions):
        self.human_source(conn, run)
        conn.execute('''INSERT INTO automation_grants(automation_id,run_id,message_id,owner_id,permissions,created_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(automation_id,run_id) DO UPDATE SET
            message_id=excluded.message_id,owner_id=excluded.owner_id,
            permissions=excluded.permissions,created_at=excluded.created_at''',
            (automation_id, run['id'], run['active_message_id'], actor, json.dumps(sorted(set(permissions))), now()))


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS automation_grants (
        automation_id TEXT NOT NULL REFERENCES automations(id),
        run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER NOT NULL REFERENCES messages(id),
        owner_id TEXT NOT NULL, permissions TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(automation_id,run_id))''')
