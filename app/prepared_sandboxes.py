"""Bounded, credential-free Modal workspaces, handed to one durable session.

Pool entries and active sessions share admission capacity. Provider calls never
hold global admission; named reservations survive restarts and lost create ACKs.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

import modal

from .db import database
from .runner import refresh_sandbox_files
from .runtime_coordination import lease
from .scheduling_diagnostics import record

log = logging.getLogger(__name__)


class PreparedSandboxes:
    def __init__(self, manager):
        self.manager, self.store, self.settings = manager, manager.store, manager.settings
        self.lock = asyncio.Lock()
        if self.store.schema_updates:
            initialize_schema(self.store)
        root = Path(__file__).resolve().parents[1]
        revision = hashlib.sha256()
        for path in sorted([*(root / 'sandbox').glob('*.py'), *(root / 'sandbox').glob('hermes-*.patch')]):
            revision.update(path.name.encode()); revision.update(path.read_bytes())
        revision.update((root / 'app/workspace_image.py').read_bytes())
        self.build = hashlib.sha256(str((self.settings.moyai_build_sha,
            self.settings.hermes_revision, self.settings.modal_app_name,
            self.settings.modal_vm_runtime, revision.hexdigest())).encode()).hexdigest()

    def eligible(self, run):
        basic = bool(self.settings.sandbox_prepared_pool_size and run
            and run['mode'] == 'modal' and run.get('sandbox_provider', 'modal') == 'modal'
            and not run.get('snapshot_id') and not run.get('repo_url')
            and not run.get('github_repository_id')
            and not run.get('environment_build_id')
            and run.get('environment_id') in ('', 'auto', None)
            and not run.get('parent_run_id') and not run.get('side_chat_of')
            and not self.store.slack_source(run['id']))
        if not basic:
            return False
        # Auto can resolve to an organization default even with no repository.
        # Such sessions must retain the normal prepare/bind/provision path.
        return not (self.manager.environments and self.manager.environments.choose(
            run.get('environment_id'), run.get('repo_url', ''), run.get('github_repository_id')))

    def ready(self, run_id):
        return self.eligible(self.store.run(run_id)) and bool(self.store.rows('''
            SELECT 1 FROM prepared_sandboxes WHERE build=? AND status='ready'
            AND expires_at>? LIMIT 1''', (self.build, time.time())))

    def count(self):
        return self.store.rows('SELECT COUNT(*) AS n FROM prepared_sandboxes')[0]['n']

    def assign(self, run_id, run, state):
        """Called under admission; transfer pool ownership with the journal."""
        if not self.eligible(run):
            return False
        with self.store.connect() as conn:
            conn.begin_write()
            row = conn.execute('''SELECT * FROM prepared_sandboxes WHERE build=?
                AND status='ready' AND expires_at>? ORDER BY created_at LIMIT 1''',
                (self.build, time.time())).fetchone()
            if not row:
                return False
            state.update(sandbox_id=row['sandbox_id'], sandbox_name=row['name'],
                         machine_started=row['created_at'], reused_machine=True,
                         prepared_workspace=True, prepared_build=self.build)
            conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
            conn.execute('UPDATE runs SET sandbox_id=? WHERE id=?', (row['sandbox_id'], run_id))
            conn.execute('DELETE FROM prepared_sandboxes WHERE name=?', (row['name'],))
        record('prepared_workspace_claimed', run_id=run_id)
        return True

    @asynccontextmanager
    async def guard(self, *, wait=True):
        if not wait and self.lock.locked():
            yield False
            return
        async with self.lock:
            if self.manager.coordinated_database:
                async with lease(self.manager.coordinated_database, 'prepared-sandboxes', wait=False) as acquired:
                    yield acquired
            else:
                yield True

    async def remove(self, row):
        # The caller owns the pool lease. Mark under admission so assign cannot
        # race termination; retain the counted reservation on ambiguous failure.
        async with self.manager.admission_lock:
            changed = await database(self.store.execute,
                "UPDATE prepared_sandboxes SET status='deleting' WHERE name=?", (row['name'],))
        if not changed:
            return False  # A session already owns this machine.
        backend = self.manager.provider(name='modal')
        try:
            sandbox = await backend.find(row['name'])
            await sandbox.terminate.aio()
            await sandbox.wait.aio(raise_on_termination=False)
        except modal.exception.NotFoundError:
            if not row.get('sandbox_id'):
                # A process may have died while create was in flight. Absence
                # does not prove that it won't appear later. Keep its name and
                # capacity reservation until lookup can confirm termination.
                return False
        async with self.manager.admission_lock:
            await database(self.store.execute, 'DELETE FROM prepared_sandboxes WHERE name=?', (row['name'],))
        return True

    async def reclaim(self):
        async with self.guard(wait=False) as acquired:
            if not acquired:
                return False
            rows = await database(self.store.rows,
                "SELECT * FROM prepared_sandboxes ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END,created_at")
            for row in rows:
                if await self.remove(row):
                    return True
            return False

    async def maintain(self):
        async with self.guard() as acquired:
            if not acquired:
                return
            rows = await database(self.store.rows, 'SELECT * FROM prepared_sandboxes ORDER BY created_at')
            size = 0 if self.settings.maintenance_drain else self.settings.sandbox_prepared_pool_size
            for index, row in enumerate(rows):
                if (row['build'] != self.build or row['expires_at'] <= time.time()
                        or row['status'] == 'deleting' or index >= size):
                    await self.remove(row)
            if not size or self.manager.closing:
                return
            rows = await database(self.store.rows,
                "SELECT * FROM prepared_sandboxes WHERE status='preparing' ORDER BY created_at LIMIT 1")
            row = rows[0] if rows else None
            if row is None:
                async with self.manager.admission_lock:
                    if (await database(self.count) >= size or not await database(self.manager.has_capacity)):
                        return
                    created = time.time()
                    row = {'name': 'moyai-ready-' + uuid4().hex, 'created_at': created,
                           'expires_at': created + self.settings.sandbox_prepared_idle_seconds}
                    await database(self.store.execute, '''INSERT INTO prepared_sandboxes
                        (name,build,status,created_at,expires_at) VALUES(?,?,'preparing',?,?)''',
                        (row['name'], self.build, row['created_at'], row['expires_at']))
            backend = self.manager.provider(name='modal')
            try:
                sandbox = await backend.find(row['name'])
            except modal.exception.NotFoundError:
                try:
                    # No task, identity, connected-app credential or run token.
                    sandbox = await backend.create(name=row['name'], token='')
                except modal.exception.AlreadyExistsError:
                    sandbox = await backend.find(row['name'])
            if await sandbox.poll.aio() is not None:
                await self.remove(row)
                return
            await refresh_sandbox_files(sandbox)
            async with self.manager.admission_lock:
                await database(self.store.execute, '''UPDATE prepared_sandboxes
                    SET status='ready',sandbox_id=? WHERE name=? AND status='preparing' ''',
                    (sandbox.object_id, row['name']))
            record('prepared_workspace_ready', duration_ms=round((time.time()-row['created_at'])*1000, 2))

    async def serve(self):
        while not self.manager.closing:
            try:
                await self.maintain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Prepared workspace maintenance will retry (%s)', type(exc).__name__)
            await asyncio.sleep(5)


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS prepared_sandboxes (
        name TEXT PRIMARY KEY, build TEXT NOT NULL, status TEXT NOT NULL,
        sandbox_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        expires_at REAL NOT NULL)''')
