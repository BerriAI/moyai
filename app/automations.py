"""Saved workflows, idempotent launches, and a durable Temporal Schedule outbox."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
                               ScheduleCalendarSpec, ScheduleOverlapPolicy, SchedulePolicy,
                               ScheduleRange, ScheduleSpec, ScheduleState, ScheduleUpdate)

from .automation_workflow import AutomationWorkflow
from .db import now

log = logging.getLogger(__name__)

LINEAR_TEMPLATE = '''Use linear_my_issues to read my open Linear tickets. Pick at most one clear, actionable ticket for the selected repository. Skip tickets already in review, blocked, or requiring product decisions. Read its details and recent comments.

Before starting work, call automation_claim_item with the Linear issue identifier as item_key. If it is already claimed by another run, skip it and try another ticket. If no suitable tickets remain, report that and stop. Do not repeat work from earlier automation runs.

Implement the fix in the repository, run the relevant tests, and prepare a normal ready-for-review PR. Include the Linear ticket link, the problem, the change, and test results. Use github_create_pull_request and its existing administrator approval flow. Do not approve, merge, enable auto-merge, or write to Slack or Linear. If access or requirements are missing, explain the blocker in this session. Finish with the PR link or the approval/blocker status.'''


class Timing(BaseModel):
    model_config = ConfigDict(extra='forbid')
    frequency: Literal['hourly', 'daily', 'weekdays', 'weekly'] = 'weekdays'
    time: str = Field(default='09:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    weekday: int = Field(default=1, ge=0, le=6)  # Sunday = 0, as in Temporal.
    timezone: str = Field(default='America/Los_Angeles', max_length=100)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Choose a valid IANA timezone, such as America/Los_Angeles.') from None
        return value

    def spec(self):
        hour, minute = map(int, self.time.split(':'))
        days = [ScheduleRange(1, 5)] if self.frequency == 'weekdays' else [ScheduleRange(self.weekday)] if self.frequency == 'weekly' else [ScheduleRange(0, 6)]
        return ScheduleSpec(time_zone_name=self.timezone, calendars=[ScheduleCalendarSpec(
            hour=[ScheduleRange(0, 23)] if self.frequency == 'hourly' else [ScheduleRange(hour)],
            minute=[ScheduleRange(minute)], day_of_week=days)])


class Definition(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=100)
    prompt: str = Field(min_length=3, max_length=14000)
    timing: Timing = Field(default_factory=Timing)
    repo_url: str = Field(default='', max_length=500)
    environment_id: str = Field(default='auto', pattern=r'^(auto|none|[0-9a-f]{32})$')
    plugins: list[Literal['linear', 'github', 'slack', 'notion']] = Field(default_factory=list, max_length=4)
    model: str = Field(default='', max_length=120)
    mode: Literal['modal', 'demo'] = 'modal'

    @field_validator('repo_url')
    @classmethod
    def repository(cls, value):
        value = value.removesuffix('/')
        if value and not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
            raise ValueError('Use a https://github.com/owner/repository URL.')
        return value


class Save(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition: Definition
    revision: int = Field(default=0, ge=0)


class Toggle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    paused: bool


class Launch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    client_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    item_key: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/-]*$')


class Automations:
    def __init__(self, store, settings, security, manager, connectors, environments, checkpoints):
        self.store, self.settings, self.security = store, settings, security
        self.manager, self.connectors, self.environments, self.checkpoints = manager, connectors, environments, checkpoints
        self.next_sync = 0
        self.task = None
        self.sync_lock = asyncio.Lock()
        store.execute('''CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), definition TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, synced_revision INTEGER NOT NULL DEFAULT 0,
            paused INTEGER NOT NULL DEFAULT 1, sync_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        store.execute('''CREATE TABLE IF NOT EXISTS automation_runs (
            occurrence TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES automations(id),
            revision INTEGER NOT NULL, run_id TEXT UNIQUE REFERENCES runs(id),
            outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''')
        store.execute('CREATE INDEX IF NOT EXISTS automation_runs_history ON automation_runs(automation_id,created_at)')
        store.execute('''CREATE TABLE IF NOT EXISTS automation_items (
            automation_id TEXT NOT NULL REFERENCES automations(id), item_key TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES runs(id), created_at TEXT NOT NULL,
            PRIMARY KEY(automation_id,item_key))''')

    def start(self):
        if self.settings.temporal_enabled:
            self.task = asyncio.create_task(self.serve())

    async def serve(self):
        while True:
            try:
                if self.manager.ready.is_set() and self.manager.temporal:
                    await self.sync(self.manager.temporal)
            except Exception as exc:
                log.warning('Automation schedule sync paused (%s); retrying', type(exc).__name__)
            await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def row(self, automation_id):
        rows = self.store.rows('SELECT * FROM automations WHERE id=?', (automation_id,))
        if not rows:
            raise HTTPException(404, 'Automation not found.')
        return rows[0]

    def actor(self, request):
        return self.store.identity(self.security.session_info(request))

    def require_owner(self, row, request, *, pausing=False):
        if row['owner_id'] != self.actor(request) and not (pausing and self.security.role(request) == 'admin'):
            raise HTTPException(403, 'Only the owner can edit or run this automation. Administrators can pause it.')

    def validate_execution(self, definition, owner_id):
        # Re-evaluate access at every launch; saved workflows confer no new rights.
        owners = self.store.rows('SELECT * FROM users WHERE id=?', (owner_id,))
        if not owners or (owners[0]['kind'] == 'google' and (not self.settings.google_enabled() or owners[0]['email'].rpartition('@')[2] not in self.settings.google_domains())):
            raise HTTPException(409, 'The automation owner no longer has workspace access.')
        if owners[0]['kind'] == 'shared' and not (self.security.local_preview() or self.settings.password_login_enabled):
            raise HTTPException(409, 'The automation owner no longer has workspace access.')
        if definition.mode == 'demo':
            return
        if not all((self.settings.modal_token_id, self.settings.modal_token_secret, self.settings.litellm_api_base, self.settings.litellm_api_key)):
            raise HTTPException(409, 'Cloud setup is incomplete. Check Runtime before running this automation.')
        enabled = {c['id'] for c in self.connectors.list() if c['connected'] and c['enabled']}
        if not set(definition.plugins) <= enabled:
            raise HTTPException(409, 'Connect and enable the selected apps before running this automation.')
        self.settings.resolve_model(definition.model)
        self.environments.choose(definition.environment_id, definition.repo_url)

    def public(self, row, actor):
        result = {key: row[key] for key in ('id', 'owner_id', 'revision', 'synced_revision', 'paused', 'sync_error', 'created_at', 'updated_at')}
        result['definition'] = json.loads(row['definition'])
        result['can_edit'] = row['owner_id'] == actor
        owner = self.store.rows('SELECT name,email FROM users WHERE id=?', (row['owner_id'],))[0]
        result['owner'] = owner['email'] or owner['name']
        result['history'] = self.store.rows('''SELECT a.occurrence,a.run_id,a.outcome,a.detail,a.created_at,
            r.status FROM automation_runs a LEFT JOIN runs r ON r.id=a.run_id
            WHERE a.automation_id=? ORDER BY a.created_at DESC LIMIT 20''', (row['id'],))
        return result

    def save(self, body, owner_id, automation_id=None):
        definition = body.definition.model_copy(update={'model': self.settings.resolve_model(body.definition.model or None),
                                                      'plugins': sorted(set(body.definition.plugins))})
        stamp = now()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if automation_id:
                row = conn.execute('SELECT * FROM automations WHERE id=?', (automation_id,)).fetchone()
                if not row or row['owner_id'] != owner_id:
                    raise HTTPException(403, 'Only the owner can edit this automation.')
                if row['revision'] != body.revision:
                    raise HTTPException(409, 'This automation changed. Refresh before saving.')
                # Editing pauses the schedule so revised instructions are reviewed before enabling.
                conn.execute('UPDATE automations SET definition=?,revision=revision+1,paused=1,updated_at=?,sync_error=\'\' WHERE id=?',
                             (definition.model_dump_json(), stamp, automation_id))
            else:
                if body.revision:
                    raise HTTPException(409, 'A new automation starts at revision zero.')
                if conn.execute('SELECT COUNT(*) FROM automations').fetchone()[0] >= 200:
                    raise HTTPException(409, 'This workspace has reached its 200 automation limit.')
                automation_id = uuid4().hex
                conn.execute('INSERT INTO automations(id,owner_id,definition,created_at,updated_at) VALUES(?,?,?,?,?)',
                             (automation_id, owner_id, definition.model_dump_json(), stamp, stamp))
        self.next_sync = 0
        return self.row(automation_id)

    async def launch(self, automation_id, revision, occurrence, expires_at='', *, manual=False):
        # No await inside the transaction: receipt + run + initial inbox + wake
        # are committed together. Retries can only return this same session.
        row = self.row(automation_id)
        definition = Definition.model_validate_json(row['definition'])
        error = ''
        try:
            self.validate_execution(definition, row['owner_id'])
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM automation_runs WHERE occurrence=?', (occurrence,)).fetchone()
            if previous:
                result = {'run_id': previous['run_id'] or '', 'outcome': previous['outcome']}
            else:
                current = conn.execute('SELECT * FROM automations WHERE id=?', (automation_id,)).fetchone()
                reason = ''
                if current['revision'] != revision or (current['paused'] and not manual):
                    reason = 'Schedule paused or changed before this occurrence started.'
                elif expires_at and datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                    reason = 'Skipped an occurrence delayed by more than 15 minutes.'
                elif error:
                    reason = error
                elif conn.execute('''SELECT 1 FROM automation_runs a JOIN runs r ON (r.id=a.run_id OR r.parent_run_id=a.run_id)
                    WHERE a.automation_id=? AND (r.status NOT IN ('idle','completed','failed','cancelled','interrupted')
                    OR EXISTS(SELECT 1 FROM messages m WHERE m.run_id=r.id AND m.status IN ('queued','running','injected')))''', (automation_id,)).fetchone():
                    reason = 'The previous run is still active or waiting for input.'
                if reason:
                    conn.execute('INSERT INTO automation_runs VALUES(?,?,?,NULL,?,?,?)',
                                 (occurrence, automation_id, revision, 'blocked' if error else 'skipped', reason, now()))
                    result = {'run_id': '', 'outcome': 'blocked' if error else 'skipped'}
                else:
                    pending = conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('idle','completed','failed','cancelled','interrupted')").fetchone()[0]
                    if pending >= self.store.max_pending_runs:
                        raise HTTPException(429, 'The session queue is full. Retry later.')
                    run_id, stamp = uuid4().hex, now()
                    prompt = definition.prompt
                    # Supply a bounded history for recurring workflows without exposing it to Temporal.
                    recent = conn.execute('''SELECT r.id,r.status,r.summary FROM automation_runs a JOIN runs r ON r.id=a.run_id
                        WHERE a.automation_id=? ORDER BY a.created_at DESC LIMIT 3''', (automation_id,)).fetchall()
                    if recent:
                        prompt += '\n\n<previous_automation_runs>\nReference only; do not replay prior instructions.\n' + json.dumps([dict(r) | {'summary': r['summary'][:1000]} for r in recent], ensure_ascii=False) + '\n</previous_automation_runs>'
                    conn.execute('''INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,
                        chat_enabled,model,active_model,owner_id,active_user_id,environment_id,agent_label)
                        VALUES(?,?,?,?,'queued',?,?,?,1,?,?,?,?,?,?)''',
                        (run_id, prompt, definition.repo_url, definition.mode, json.dumps(definition.plugins), stamp, stamp,
                         definition.model, definition.model, row['owner_id'], row['owner_id'], definition.environment_id, 'Automation · ' + definition.name))
                    conn.execute("INSERT INTO messages(run_id,role,content,status,client_id,created_at,model,user_id) VALUES(?,'user',?,'queued','initial',?,?,?)",
                                 (run_id, prompt, stamp, definition.model, row['owner_id']))
                    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'status','Started by automation','{}',?)", (run_id, stamp))
                    conn.execute('INSERT INTO automation_runs VALUES(?,?,?,?,?,?,?)', (occurrence, automation_id, revision, run_id, 'started', '', stamp))
                    if self.settings.temporal_enabled:
                        conn.execute('INSERT INTO durable_sessions(run_id,revision) VALUES(?,1)', (run_id,))
                    result = {'run_id': run_id, 'outcome': 'started'}
        await self.checkpoints.flush()
        if result['run_id'] and not self.settings.temporal_enabled:
            self.manager.submit(self.store.run(result['run_id']))
        return result

    def finished(self, run_id):
        run = self.store.run(run_id)
        if not run:
            raise RuntimeError('Scheduled session is unavailable')
        return not self.store.rows("""SELECT 1 FROM runs r WHERE (r.id=? OR r.parent_run_id=?) AND
            (r.status NOT IN ('idle','completed','failed','cancelled','interrupted') OR EXISTS(
                SELECT 1 FROM messages WHERE run_id=r.id AND status IN ('queued','running','injected')))""", (run_id, run_id))

    def tools(self, run):
        if not self.store.rows('SELECT 1 FROM automation_runs WHERE run_id=?', (run['id'],)):
            return []
        return [{'name': 'automation_claim_item', 'description': 'Reserve a ticket or work item for this automation before acting. Use the stable issue identifier as item_key. An item claimed by another run must be skipped, even if that run failed; continue or review that original session instead of creating another PR.',
                 'inputSchema': Claim.model_json_schema()}]

    def claim(self, run, arguments):
        key = Claim.model_validate(arguments).item_key.lower()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            source = conn.execute('SELECT automation_id FROM automation_runs WHERE run_id=?', (run['id'],)).fetchone()
            if not source:
                raise HTTPException(403, 'This tool is available only in automation sessions.')
            conn.execute('INSERT OR IGNORE INTO automation_items VALUES(?,?,?,?)', (source['automation_id'], key, run['id'], now()))
            item = conn.execute('SELECT run_id FROM automation_items WHERE automation_id=? AND item_key=?', (source['automation_id'], key)).fetchone()
        return {'claimed': item['run_id'] == run['id'], 'run_id': item['run_id']}

    def schedule(self, row):
        definition = Definition.model_validate_json(row['definition'])
        return Schedule(
            action=ScheduleActionStartWorkflow(AutomationWorkflow.run, args=[row['id'], row['revision']],
                id='moyai-automation-tick-' + row['id'], task_queue=self.settings.temporal_task_queue),
            spec=definition.timing.spec(), policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=timedelta(minutes=15)), state=ScheduleState(paused=bool(row['paused'])))

    async def sync(self, client):
        if asyncio.get_running_loop().time() < self.next_sync or self.sync_lock.locked():
            return
        async with self.sync_lock:
            self.next_sync = asyncio.get_running_loop().time() + 15
            for row in self.store.rows('SELECT * FROM automations WHERE revision>synced_revision ORDER BY updated_at LIMIT 20'):
                try:
                    value = self.schedule(row)
                    schedule_id = 'moyai-automation-' + row['id']
                    try:
                        await client.create_schedule(schedule_id, value, rpc_timeout=timedelta(seconds=10))
                    except ScheduleAlreadyRunningError:
                        await client.get_schedule_handle(schedule_id).update(lambda _, value=value: ScheduleUpdate(value), rpc_timeout=timedelta(seconds=10))
                    self.store.execute("UPDATE automations SET synced_revision=?,sync_error='' WHERE id=? AND revision=?", (row['revision'], row['id'], row['revision']))
                except Exception:
                    # SDK diagnostics may include credentials; expose only a safe message.
                    self.store.execute('UPDATE automations SET sync_error=? WHERE id=? AND revision=?',
                                       ('Schedule sync unavailable. Changes are saved and will retry.', row['id'], row['revision']))

    def routes(self):
        router = APIRouter()

        @router.get('/api/automations')
        async def listing(request: Request):
            self.security.require(request)
            actor = self.actor(request)
            return {'enabled': self.settings.temporal_enabled, 'connected': bool(getattr(self.manager, 'ready', None) and self.manager.ready.is_set()),
                    'automations': [self.public(row, actor) for row in self.store.rows('SELECT * FROM automations ORDER BY updated_at DESC')],
                    'templates': [{'id': 'linear-pr', 'name': 'My Linear tickets → PR', 'prompt': LINEAR_TEMPLATE, 'plugins': ['linear', 'github']}]}

        @router.post('/api/automations', status_code=201)
        async def create(body: Save, request: Request):
            self.security.require(request, mutation=True)
            try:
                row = self.save(body, self.actor(request))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            await self.checkpoints.flush()
            return self.public(row, row['owner_id'])

        @router.put('/api/automations/{automation_id}')
        async def edit(automation_id: str, body: Save, request: Request):
            self.security.require(request, mutation=True)
            self.require_owner(self.row(automation_id), request)
            try:
                row = self.save(body, self.actor(request), automation_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            await self.checkpoints.flush()
            return self.public(row, row['owner_id'])

        @router.post('/api/automations/{automation_id}/state')
        async def toggle(automation_id: str, body: Toggle, request: Request):
            self.security.require(request, mutation=True)
            row = self.row(automation_id)
            self.require_owner(row, request, pausing=body.paused)
            if not body.paused:
                if not self.settings.temporal_enabled:
                    raise HTTPException(409, 'Enable Temporal before enabling scheduled runs.')
                try:
                    self.validate_execution(Definition.model_validate_json(row['definition']), row['owner_id'])
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
            changed = self.store.execute("UPDATE automations SET paused=?,revision=revision+1,updated_at=?,sync_error='' WHERE id=? AND revision=?", (body.paused, now(), automation_id, body.revision))
            if not changed:
                raise HTTPException(409, 'This automation changed. Refresh and try again.')
            self.next_sync = 0
            await self.checkpoints.flush()
            return self.public(self.row(automation_id), self.actor(request))

        @router.post('/api/automations/{automation_id}/run', status_code=202)
        async def run_now(automation_id: str, body: Launch, request: Request):
            self.security.require(request, mutation=True)
            self.require_owner(self.row(automation_id), request)
            return await self.launch(automation_id, body.revision, 'manual:' + automation_id + ':' + body.client_id, manual=True)

        return router
