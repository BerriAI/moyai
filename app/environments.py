"""Versioned organization environments. Builds run in isolated Modal sandboxes.

The database journals build phases; the detached build process and named sandbox
survive web-worker restarts. Only validated snapshots are published to sessions.
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

import modal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now
from .environment_templates import TEMPLATES
from .connector_errors import ConnectorError


class Recipe(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=80)
    repository: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
    ref: str = Field(default='main', min_length=1, max_length=200, pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]*$')
    clone_access: Literal['public', 'github'] = 'public'
    apt_packages: list[str] = Field(default_factory=list, max_length=40)
    setup: str = Field(default='', max_length=24000)
    startup: str = Field(default='', max_length=12000)
    verify: str = Field(min_length=1, max_length=12000)
    shutdown: str = Field(default='', max_length=6000)
    instructions: str = Field(default='', max_length=12000)


class SaveRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipe: Recipe
    revision: int = Field(default=0, ge=0)


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    is_default: bool = False
    refresh_daily: bool | None = None


class Environments:
    def __init__(self, store, settings, security, manager, connectors, checkpoints):
        self.store, self.settings, self.security = store, settings, security
        self.manager, self.connectors, self.checkpoints = manager, connectors, checkpoints
        self.task = None
        store.execute('''CREATE TABLE IF NOT EXISTS environments (
            id TEXT PRIMARY KEY, recipe TEXT NOT NULL, revision INTEGER NOT NULL,
            active_build TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        store.execute('''CREATE TABLE IF NOT EXISTS environment_builds (
            id TEXT PRIMARY KEY, environment_id TEXT NOT NULL REFERENCES environments(id),
            revision INTEGER NOT NULL, recipe TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
            sandbox_id TEXT NOT NULL DEFAULT '', snapshot_id TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL DEFAULT '', log TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL)''')
        with store.connect() as conn:
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(environments)')}
            for name in ('refresh_daily', 'activate_on_ready'):
                if name not in columns:
                    conn.execute(f'ALTER TABLE environments ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')

    def bootstrap(self):
        """Queue first-time setup atomically; never overwrite an admin's choices."""
        if not (self.settings.auto_setup_litellm_environment and self.settings.modal_token_id
                and self.settings.modal_token_secret):
            return
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM environments LIMIT 1').fetchone():
                return
            identity, build_id, stamp = uuid4().hex, uuid4().hex, now()
            recipe = Recipe.model_validate(TEMPLATES[0]).model_dump_json()
            conn.execute('''INSERT INTO environments(id,recipe,revision,activate_on_ready,updated_by,updated_at)
                VALUES(?,?,1,1,?,?)''', (identity, recipe, 'Automatic setup', stamp))
            conn.execute('''INSERT INTO environment_builds(id,environment_id,revision,recipe,created_at,actor)
                VALUES(?,?,1,?,?,?)''', (build_id, identity, recipe, stamp, 'Automatic setup'))

    def get(self, identity):
        rows = self.store.rows('SELECT * FROM environments WHERE id=?', (identity,))
        if not rows:
            raise HTTPException(404, 'Project environment not found.')
        return rows[0]

    def catalog(self, admin=False):
        items = []
        for row in self.store.rows('SELECT * FROM environments ORDER BY updated_at DESC,id'):
            recipe = json.loads(row['recipe'])
            value = {k: row[k] for k in ('id', 'revision', 'active_build', 'enabled', 'is_default', 'updated_at', 'refresh_daily')}
            value.update(name=recipe['name'], repository=recipe['repository'])
            if not admin and row['active_build']:
                active_recipe = json.loads(self.build(row['active_build'])['recipe'])
                value.update(name=active_recipe['name'], repository=active_recipe['repository'])
            if admin:
                value['activate_on_ready'] = row['activate_on_ready']
                value['recipe'] = recipe
                value['builds'] = self.store.rows('''SELECT id,revision,phase,commit_sha,error,created_at,finished_at
                    FROM environment_builds WHERE environment_id=? ORDER BY rowid DESC LIMIT 10''', (row['id'],))
            if admin or (row['enabled'] and row['active_build']):
                items.append(value)
        return items

    def save_recipe(self, identity, body, actor):
        import re
        if any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,79}', p) for p in body.recipe.apt_packages):
            raise HTTPException(422, 'Use Debian package names without flags or versions.')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM environments WHERE id=?', (identity,)).fetchone()
            if (old['revision'] if old else 0) != body.revision:
                raise HTTPException(409, 'This environment changed. Reload before saving.')
            # An edit retains the last good build until the new revision validates.
            conn.execute('''INSERT INTO environments(id,recipe,revision,updated_by,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET recipe=excluded.recipe,revision=excluded.revision,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at,activate_on_ready=0''',
                         (identity, body.recipe.model_dump_json(), body.revision + 1, actor, now()))
        saved = self.get(identity)
        if saved['enabled'] and saved['refresh_daily']:
            self.enqueue(identity, saved['revision'], actor)
        return saved

    def enqueue(self, identity, revision, actor):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM environments WHERE id=?', (identity,)).fetchone()
            if not row:
                raise HTTPException(404, 'Project environment not found.')
            if row['revision'] != revision:
                raise HTTPException(409, 'Save or reload the current recipe before building.')
            active = conn.execute("SELECT * FROM environment_builds WHERE environment_id=? AND phase NOT IN ('ready','failed')", (identity,)).fetchone()
            if active:
                return dict(active)
            identity_build = uuid4().hex
            conn.execute('''INSERT INTO environment_builds(id,environment_id,revision,recipe,created_at,actor)
                VALUES(?,?,?,?,?,?)''', (identity_build, identity, revision, row['recipe'], now(), actor))
        return self.build(identity_build)

    def build(self, identity):
        rows = self.store.rows('SELECT * FROM environment_builds WHERE id=?', (identity,))
        if not rows:
            raise HTTPException(404, 'Build not found.')
        return rows[0]

    def update(self, identity, **fields):
        self.store.execute('UPDATE environment_builds SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), identity))

    def progress(self, identity, **fields):
        return self.store.execute('UPDATE environment_builds SET ' + ','.join(k + '=?' for k in fields) + " WHERE id=? AND phase!='cancelling'", (*fields.values(), identity))

    def choose(self, selection, repo_url):
        if selection == 'none':
            return None
        if selection and selection != 'auto':
            row = self.get(selection)
            if not row['enabled'] or not row['active_build']:
                raise HTTPException(409, 'Build and enable this environment before starting a session.')
            recipe = json.loads(self.build(row['active_build'])['recipe'])
            if repo_url and repo_url.removeprefix('https://github.com/').removesuffix('.git').lower() != recipe['repository'].lower():
                raise HTTPException(422, 'The repository does not match the selected environment.')
            return row
        rows = self.store.rows("SELECT * FROM environments WHERE enabled=1 AND active_build!='' ORDER BY is_default DESC,updated_at DESC,id")
        repository = repo_url.removeprefix('https://github.com/').removesuffix('.git').lower()
        for row in rows:
            recipe = json.loads(self.build(row['active_build'])['recipe'])
            if (repository and repository == recipe['repository'].lower()) or (not repository and row['is_default']):
                return row
        return None

    def bind(self, run_id):
        """Pin once. Existing session snapshots always win over project templates."""
        run = self.store.run(run_id)
        if not run.get('environment_build_id'):
            previous = run.get('snapshot_id') or run.get('parent_run_id') or self.store.rows(
                "SELECT 1 FROM messages WHERE run_id=? AND role='assistant' LIMIT 1", (run_id,))
            row = None if previous else self.choose(run.get('environment_id', 'auto'), run['repo_url'])
            identity = row['active_build'] if row else 'none'
            self.store.execute("UPDATE runs SET environment_build_id=? WHERE id=? AND environment_build_id=''", (identity, run_id))
            if row:
                recipe = json.loads(self.build(identity)['recipe'])
                self.store.event(run_id, 'status', 'Using prepared environment: ' + recipe['name'], {'environment_build_id': identity})
        return self.context(self.store.run(run_id))

    def context(self, run):
        identity = run.get('environment_build_id')
        if not identity or identity == 'none':
            return {}
        build = self.build(identity)
        return {**json.loads(build['recipe']), 'build_id': identity, 'snapshot_id': build['snapshot_id'], 'commit_sha': build['commit_sha']}

    async def rpc(self, sandbox, action, *, token=''):
        proc = await sandbox.exec.aio('/usr/local/bin/python', '/opt/workspace-runner/environment_build.py', action,
                                      timeout=30, env={'MOYAI_CLONE_TOKEN': token} if token else {})
        output, _ = await asyncio.gather(proc.stdout.read.aio(), proc.stderr.read.aio())
        if await proc.wait.aio() != 0:
            raise RuntimeError('Environment supervisor unavailable')
        return json.loads(output)

    async def advance(self, build):
        identity, recipe = build['id'], json.loads(build['recipe'])
        client = await self.manager.client()
        name = 'moyai-environment-' + identity
        if build['phase'] in {'ready', 'failed', 'cancelling'}:
            try:
                sandbox = await modal.Sandbox.from_name.aio(self.settings.modal_app_name, name, client=client)
                await sandbox.terminate.aio()
            except modal.exception.NotFoundError:
                pass
            fields = {'phase': 'failed', 'error': 'Build cancelled by an administrator.', 'finished_at': now()} if build['phase'] == 'cancelling' else ({'error': ''} if build['phase'] == 'ready' else {})
            self.update(identity, sandbox_id='', **fields)
            return
        try:
            sandbox = await modal.Sandbox.from_name.aio(self.settings.modal_app_name, name, client=client)
        except modal.exception.NotFoundError:
            if build['phase'] != 'queued':
                self.update(identity, phase='failed', error='The build sandbox expired. Start a new build; the previous environment is unchanged.', finished_at=now())
                return
            app = await modal.App.lookup.aio(self.settings.modal_app_name, create_if_missing=True, client=client)
            sandbox = await modal.Sandbox.create.aio(app=app, client=client, name=name,
                image=self.manager.image().apt_install(*recipe['apt_packages']) if recipe['apt_packages'] else self.manager.image(),
                timeout=3600, cpu=2, memory=8192)
        self.update(identity, sandbox_id=sandbox.object_id)
        if build['phase'] == 'queued':
            await sandbox.filesystem.write_text.aio(json.dumps(recipe), '/tmp/moyai-environment.json')
            token = ''
            # Use a narrowly scoped read token only for the clone subprocess.
            if recipe.get('clone_access') == 'github':
                if not any(c['id'] == 'github' and c['connected'] and c['enabled'] for c in self.connectors.list()):
                    raise ConnectorError('Enable the shared GitHub connection before building this private repository.')
                target = await self.connectors.github.selected_target({'repo_url': 'https://github.com/' + recipe['repository']}, recipe['repository'])
                token = await self.connectors.github.installation_token(repository=target)
            await self.rpc(sandbox, 'start', token=token)
            if not self.progress(identity, phase='building'):
                return
        report = await self.rpc(sandbox, 'status')
        self.update(identity, log=str(report.get('log', ''))[-48000:])
        if not report.get('done'):
            return
        if not report.get('success'):
            self.update(identity, phase='failed', error=report.get('error', 'Setup or validation failed.'), finished_at=now())
            await sandbox.terminate.aio()
            return
        if not self.progress(identity, phase='saving', commit_sha=report['commit_sha']):
            return
        snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Respect a cancellation that arrived while snapshotting.
            if conn.execute('SELECT phase FROM environment_builds WHERE id=?', (identity,)).fetchone()['phase'] == 'cancelling':
                return
            conn.execute("UPDATE environment_builds SET phase='ready',snapshot_id=?,finished_at=?,error='' WHERE id=?", (snapshot.object_id, now(), identity))
            # A build of an older edited revision never takes over the default.
            conn.execute('UPDATE environments SET active_build=?,updated_at=? WHERE id=? AND revision=?',
                         (identity, now(), build['environment_id'], build['revision']))
            row = conn.execute('SELECT * FROM environments WHERE id=?', (build['environment_id'],)).fetchone()
            if row['activate_on_ready'] and row['revision'] == build['revision']:
                has_default = conn.execute('SELECT 1 FROM environments WHERE is_default=1 AND enabled=1 LIMIT 1').fetchone()
                conn.execute('UPDATE environments SET enabled=1,is_default=?,activate_on_ready=0 WHERE id=?',
                             (not bool(has_default), row['id']))
        await self.checkpoints.flush()
        await sandbox.terminate.aio()
        self.update(identity, sandbox_id='')

    async def watch(self):
        while True:
            self.queue_refreshes()
            builds = self.store.rows("SELECT * FROM environment_builds WHERE phase NOT IN ('ready','failed') OR sandbox_id!='' ORDER BY rowid LIMIT 1")
            for build in builds:
                try:
                    if build['phase'] not in {'ready', 'failed', 'cancelling'} and time.time() - datetime.fromisoformat(build['created_at']).timestamp() > 3900:
                        self.update(build['id'], phase='failed', error='Build timed out. Edit the recipe or retry; the last good environment is unchanged.', finished_at=now())
                    else:
                        await self.advance(build)
                except asyncio.CancelledError:
                    raise  # Leave the named sandbox and journal for the next worker.
                except ConnectorError as exc:
                    self.update(build['id'], phase='failed', error=str(exc), finished_at=now())
                except Exception as exc:
                    # Transient provider errors are retried against the same sandbox.
                    self.update(build['id'], error=f'Build service reconnecting ({type(exc).__name__}); retrying.')
                await self.checkpoints.flush()
            await asyncio.sleep(3)

    def queue_refreshes(self):
        for row in self.store.rows('SELECT * FROM environments WHERE enabled=1 AND refresh_daily=1'):
            recent = self.store.rows('SELECT revision,created_at FROM environment_builds WHERE environment_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],))
            if (not recent or recent[0]['revision'] != row['revision'] or
                    time.time() - datetime.fromisoformat(recent[0]['created_at']).timestamp() >= 86400):
                self.enqueue(row['id'], row['revision'], 'Automatic refresh')

    def start(self):
        self.bootstrap()
        self.task = asyncio.create_task(self.watch())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def routes(self):
        router = APIRouter()

        def actor(request):
            self.security.require(request, admin=True, mutation=True)
            info = self.security.session_info(request)
            return info.get('identity', {}).get('email') or info.get('method', 'admin')

        @router.get('/api/environments')
        async def catalog(request: Request):
            self.security.require(request)
            return self.catalog()

        @router.get('/api/admin/environments')
        async def administration(request: Request):
            self.security.require(request, admin=True)
            return {'environments': self.catalog(admin=True), 'templates': TEMPLATES,
                    'automatic_setup': self.settings.auto_setup_litellm_environment,
                    'modal_configured': bool(self.settings.modal_token_id and self.settings.modal_token_secret)}

        @router.post('/api/admin/environments', status_code=201)
        async def create(body: SaveRecipe, request: Request):
            return self.save_recipe(uuid4().hex, body, actor(request))

        @router.put('/api/admin/environments/{identity}')
        async def save(identity: str, body: SaveRecipe, request: Request):
            who = actor(request)
            self.get(identity)
            return self.save_recipe(identity, body, who)

        @router.post('/api/admin/environments/{identity}/build', status_code=202)
        async def build(identity: str, body: BuildRequest, request: Request):
            who = actor(request)
            if not self.settings.modal_token_id or not self.settings.modal_token_secret:
                raise HTTPException(503, 'Configure Modal in Runtime before building an environment.')
            return self.enqueue(identity, body.revision, who)

        @router.put('/api/admin/environments/{identity}/policy')
        async def policy(identity: str, body: Policy, request: Request):
            actor(request)
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM environments WHERE id=?', (identity,)).fetchone()
                if not row:
                    raise HTTPException(404, 'Project environment not found.')
                if body.enabled and not row['active_build']:
                    raise HTTPException(409, 'A successful build is required before enabling an environment.')
                if body.is_default and not body.enabled:
                    raise HTTPException(422, 'Enable an environment before making it the default.')
                if body.is_default:
                    conn.execute('UPDATE environments SET is_default=0')
                conn.execute('UPDATE environments SET enabled=?,is_default=?,refresh_daily=?,activate_on_ready=0 WHERE id=?',
                             (body.enabled, body.is_default, row['refresh_daily'] if body.refresh_daily is None else body.refresh_daily, identity))
            return {'ok': True}

        @router.get('/api/admin/environment-builds/{identity}')
        async def logs(identity: str, request: Request):
            self.security.require(request, admin=True)
            return self.build(identity)

        @router.post('/api/admin/environment-builds/{identity}/cancel')
        async def cancel(identity: str, request: Request):
            actor(request)
            self.build(identity)
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute("UPDATE environment_builds SET phase='cancelling' WHERE id=? AND phase NOT IN ('ready','failed')", (identity,)).rowcount:
                    conn.execute('UPDATE environments SET activate_on_ready=0 WHERE id=(SELECT environment_id FROM environment_builds WHERE id=?)', (identity,))
            return {'ok': True}

        return router
