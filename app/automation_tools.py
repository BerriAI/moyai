"""Owner-scoped chat controls over the existing automation store and scheduler."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .automations import Definition, Save, Toggle, SCHEDULE_VERSION
from .automation_events import WebhookSetup
from .connector_errors import ConnectorError


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Listing(Arguments):
    automation_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class Mutation(Arguments):
    turn_id: int = Field(ge=1, description='Current turn_id returned by automation_list; list first.')
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$', description='Stable key for this action, including identical retries across follow-ups.')


class Create(Mutation):
    definition: Definition


class Target(Mutation):
    automation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    revision: int = Field(ge=1, description='Current revision from automation_list or the last successful action.')


class Update(Target):
    definition: Definition


class WebhookRead(Arguments):
    automation_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class WebhookConfigure(Target):
    provider: str = Field(min_length=1, max_length=32)
    credential_request_id: str = Field(pattern=r'^[0-9a-f]{32}$', description='Authorized persistent generic webhook-signing-secret request containing WEBHOOK_SECRET. Never pass a secret value.')


SPECS = {
    'automation_webhook_info': (WebhookRead, 'Inspect the current requester’s automation receiver URLs, provider configuration and recent deliveries. Readiness means a receiver secret exists, not that a provider webhook has been registered. Never returns secrets.'),
    'automation_webhook_setup': (WebhookConfigure, 'Configure or rotate a webhook RECEIVER owned by the current requester. First obtain a non-expiring persistent generic credential using credentials_request, name=webhook-signing-secret, format=env, input_fields=[{name: WEBHOOK_SECRET, label: Webhook signing secret}]. Pass its request_id only. The user must permit persistent reuse: this copies the secret into durable automation configuration. Configuring pauses the automation and increments its revision. It affects all triggers for that provider. Register the same secret and returned callback URL at the provider separately; this tool does not register GitHub or other remote webhooks. Use the current revision and a stable request_key for identical retries. No secrets are returned.'),
    'automation_list': (Listing, 'List the current requester’s saved automations, or read one by automation_id. Returns the current turn_id, revisions, and scheduler-confirmed next runs when available. List before creating to avoid duplicates, and before changing an existing automation. Results may be shared in the current chat; summarize only what the user requested.'),
    'automation_create': (Create, 'Save a new automation for the current requester using the existing scheduler. Starts paused. Use only when the user asks for recurring or future work. Specify the requested cadence and timezone, repository, instructions and connections. Check github_repositories for repository access. List first to reuse an existing automation. To fulfill a request to schedule work, call automation_enable after saving; no extra confirmation is needed when the user already authorized the schedule. Reuse request_key for identical retries.'),
    'automation_update': (Update, 'Replace an automation definition owned by the current requester, using its current revision. Read it first and preserve fields the user did not ask to change. Editing pauses it; enable the returned revision when the user authorized continued scheduling. Never change someone else’s automation. Reuse request_key for identical retries.'),
    'automation_enable': (Target, 'Enable a saved automation owned by the current requester when the user has requested scheduled work. Uses existing runtime, connection and webhook checks. Report the returned state and next_run_at; pending_sync or scheduler_unavailable does not confirm a scheduled run. Do not invent the next execution time. Reuse request_key for identical retries.'),
    'automation_pause': (Target, 'Pause future launches of an automation owned by the current requester when asked. Does not stop a run already in progress. Requires the current revision; reuse request_key for identical retries.'),
}
TOOL_NAMES = set(SPECS)


class AutomationTools:
    def __init__(self, automations, same_requester, credentials=None):
        self.credentials = credentials
        self.service = automations
        self.store = automations.store
        self.same_requester = same_requester
        self.store.execute('''CREATE TABLE IF NOT EXISTS automation_operations (
            owner_id TEXT NOT NULL, run_id TEXT NOT NULL, request_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL, automation_id TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(owner_id,run_id,request_key))''')

    def actor(self, run, turn_id=None):
        run = self.store.run(run['id'])
        if (not run or not run['chat_enabled'] or not run['active_user_id'] or not run['active_message_id']
                or run['status'] not in {'running', 'reconnecting', 'awaiting_approval'}):
            raise HTTPException(403, 'Automation controls require an authenticated chat turn.')
        if run.get('parent_run_id') or self.store.rows('SELECT 1 FROM automation_runs WHERE run_id=?', (run['id'],)):
            raise HTTPException(403, 'Manage schedules from a direct user chat, not a delegated or automated run.')
        if turn_id is not None and turn_id != run['active_message_id']:
            raise HTTPException(409, 'The requester or turn changed. Use automation_list again.')
        actor = run['active_user_id']
        security = self.service.security
        if security.local_preview() and actor == 'shared:local:admin':
            return run, actor
        matches = [u for u in self.store.rows("SELECT id,email FROM users WHERE kind IN ('google','cloudflare')")
                   if self.same_requester(u['id'], actor)]
        if (not security.settings.person_login_enabled() or len(matches) != 1
                or matches[0]['email'].rpartition('@')[2] not in security.settings.google_domains()):
            raise HTTPException(403, 'Sign in with Google first. Slack scheduling also needs a fresh verified matching profile.')
        return run, matches[0]['id']

    def unchanged(self, run, actor):
        fresh, current_actor = self.actor(run, run['active_message_id'])
        if current_actor != actor or fresh['active_user_id'] != run['active_user_id']:
            raise HTTPException(409, 'The requester changed. Use automation_list again.')

    def tools(self, run):
        try:
            self.actor(run)
        except HTTPException:
            return []
        return [{'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
                 'annotations': {'readOnlyHint': name in {'automation_list', 'automation_webhook_info'}}}
                for name, (schema, description) in SPECS.items()]

    def owned(self, automation_id, actor):
        rows = self.store.rows('SELECT * FROM automations WHERE id=? AND owner_id=?', (automation_id, actor))
        if not rows:
            raise HTTPException(404, 'Automation not found for the current requester.')
        return rows[0]

    async def check_scope(self, run, definition):
        if not set(definition.plugins) <= set(run['plugins']):
            raise HTTPException(403, 'Select the required connections for this chat before scheduling work with them.')
        if definition.mode != run['mode']:
            raise HTTPException(403, 'Use the current chat’s execution mode for its automation.')
        try:
            await self.service.identify_repositories(definition)
        except ConnectorError as exc:
            raise HTTPException(409, str(exc)) from None

    async def result(self, row, *, synchronize=False):
        """Only Temporal's describe response may supply a confirmed next run."""
        service = self.service
        state = {'status': 'paused' if row['paused'] else 'pending_sync', 'next_run_at': None, 'next_runs': []}
        client = getattr(service.manager, 'temporal', None)
        if not row['paused']:
            if not service.settings.temporal_enabled or client is None:
                state['status'] = 'scheduler_unavailable'
            else:
                try:
                    async with asyncio.timeout(12):
                        if synchronize:
                            await service.sync(client, automation_id=row['id'])
                        current = service.row(row['id'])
                        if (current['revision'] == row['revision'] and current['synced_revision'] == row['revision']
                                and current['synced_schedule_version'] == SCHEDULE_VERSION):
                            definition = Definition.model_validate_json(row['definition'])
                            future = []
                            for trigger in definition.triggers:
                                if not trigger.schedule:
                                    continue
                                schedule_id = 'moyai-automation-' + row['id'] + ('' if trigger.id == 'default' else '-' + trigger.id)
                                remote = await client.get_schedule_handle(schedule_id).describe(rpc_timeout=timedelta(seconds=3))
                                if remote.schedule.state.paused:
                                    if trigger.schedule.frequency == 'once' and self.store.rows('SELECT 1 FROM automation_runs WHERE occurrence=?', (service.once_id(row['id'], trigger),)):
                                        continue
                                    raise RuntimeError('Schedule is still paused')
                                times = [t for t in remote.info.next_action_times if t > datetime.now(timezone.utc)]
                                if times:
                                    due = min(times)
                                    future.append({'trigger_id': trigger.id, 'at': due.astimezone(timezone.utc).isoformat(),
                                                   'local_time': due.astimezone(ZoneInfo(trigger.schedule.timezone)).isoformat(),
                                                   'timezone': trigger.schedule.timezone})
                            state = {'status': 'enabled' if future else 'enabled_no_upcoming_schedule',
                                     'next_run_at': min((v['at'] for v in future), default=None), 'next_runs': future}
                except Exception:
                    # Provider diagnostics can include private connection data.
                    state = {'status': 'pending_sync', 'next_run_at': None, 'next_runs': []}
        current = service.row(row['id'])
        if current['revision'] != row['revision']:
            state = {'status': 'paused' if current['paused'] else 'pending_sync', 'next_run_at': None, 'next_runs': []}
        result = {key: current[key] for key in ('id', 'revision', 'paused', 'synced_revision', 'sync_error')}
        return {**result, 'definition': Definition.model_validate_json(current['definition']).model_dump(mode='json'),
                **state, 'note': 'Next run times are planned scheduler times; rate limits, capacity, access checks or service outages can delay or skip execution.'}

    def webhook_info(self, row, actor):
        data = self.service.events.public(row, actor)
        return {'automation_id': row['id'], 'revision': row['revision'], 'paused': bool(row['paused']),
                'receiver': data,
                'provider_registration': 'not_verified',
                'instruction': 'Receiver readiness does not verify remote registration. Configure the callback URL and the same signing secret at the provider, then enable the returned revision. Revoking the source credential does not rotate an already configured receiver.'}

    def webhook_secret(self, conn, run, request_id):
        """Resolve only the dedicated persistent capability; never expose material."""
        if self.credentials is None:
            raise HTTPException(503, 'Secure credential access is unavailable.')
        vault = self.credentials
        request, secret = vault.authorized_request(conn, run, request_id)
        if (request['status'] != 'provided' or not secret
                or not vault.permitted(secret, run, run['active_user_id'])
                or vault.status(secret) != 'active'):
            raise HTTPException(403, 'Request active webhook signing credentials for this requester.')
        if (secret['provider'] != 'generic' or secret['name'] != 'webhook-signing-secret'
                or secret['format'] != 'env' or secret['lifetime'] != 'persistent' or secret['expires_at']):
            raise HTTPException(422, 'Use non-expiring persistent generic webhook-signing-secret credentials in env format.')
        try:
            values = json.loads(vault.security.decrypt(secret['encrypted']))
            value = values.get('WEBHOOK_SECRET') if isinstance(values, dict) else None
            if not isinstance(value, str) or not 16 <= len(value) <= 512:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, 'The credential must contain WEBHOOK_SECRET with 16 to 512 characters.') from None
        return value

    async def call(self, run, name, arguments):
        args = SPECS[name][0].model_validate(arguments)
        run, actor = self.actor(run, getattr(args, 'turn_id', None))
        if name == 'automation_webhook_info':
            result = self.webhook_info(self.owned(args.automation_id, actor), actor)
            self.unchanged(run, actor)
            return result
        if name == 'automation_list':
            if args.automation_id:
                rows = [self.owned(args.automation_id, actor)]
            else:
                rows = self.store.rows('SELECT * FROM automations WHERE owner_id=? ORDER BY created_at,id LIMIT ? OFFSET ?',
                                       (actor, args.limit + 1, args.offset))
            more = not args.automation_id and len(rows) > args.limit
            results = []
            for row in rows[:args.limit]:
                # Summaries avoid returning every saved prompt to a shared Slack chat.
                if args.automation_id:
                    results.append(await self.result(row, synchronize=True))
                else:
                    definition = Definition.model_validate_json(row['definition'])
                    results.append({**{k: row[k] for k in ('id', 'revision', 'paused', 'synced_revision', 'sync_error')},
                                    'name': definition.name, 'repo_url': definition.repo_url,
                                    'triggers': [t.model_dump(mode='json') for t in definition.triggers]})
            self.unchanged(run, actor)
            return {'turn_id': run['active_message_id'], 'automations': results,
                    'next_offset': args.offset + args.limit if more else None,
                    'instruction': 'Read an automation by automation_id for its full definition and confirmed next run before editing.'}

        row = self.owned(args.automation_id, actor) if isinstance(args, Target) else None
        if name in {'automation_create', 'automation_update', 'automation_enable'}:
            definition = args.definition if hasattr(args, 'definition') else Definition.model_validate_json(row['definition'])
            await self.check_scope(run, definition)
        # Hash supplied fields, not generated trigger IDs; identical create retries must match.
        fingerprint = json.loads(json.dumps({k: v for k, v in arguments.items() if k != 'turn_id'}))
        if hasattr(args, 'definition'):
            saved = fingerprint['definition']
            if args.definition.github_repository_id:
                saved['github_repository_id'] = args.definition.github_repository_id
                saved.pop('repo_url', None)
            supplied_events = ([saved['event']] if saved.get('event') else
                               [t.get('event') for t in saved.get('triggers', [])])
            for supplied, resolved in zip(supplied_events, args.definition.triggers):
                if supplied and resolved.event and resolved.event.provider == 'github':
                    supplied['repository_id'] = resolved.event.repository_id
                    supplied.pop('repository', None)
        digest = hashlib.sha256(json.dumps({'name': name, 'arguments': fingerprint},
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            fresh, fresh_actor = self.actor(run, args.turn_id)
            if fresh_actor != actor or fresh['active_user_id'] != run['active_user_id']:
                raise HTTPException(409, 'The requester changed. Use automation_list again.')
            operation = (actor, run['id'], args.request_key)
            prior = conn.execute('SELECT * FROM automation_operations WHERE owner_id=? AND run_id=? AND request_key=?', operation).fetchone()
            if prior:
                if prior['fingerprint'] != digest:
                    raise HTTPException(409, 'This request_key was used for a different action. List the automation before retrying.')
                row = self.owned(prior['automation_id'], actor)
                if row['revision'] != prior['revision']:
                    raise HTTPException(409, 'This action succeeded earlier, but the automation has since changed. List it again.')
            else:
                if name in {'automation_create', 'automation_update'}:
                    row = self.service.save(Save(definition=args.definition, revision=getattr(args, 'revision', 0)), actor,
                                            getattr(args, 'automation_id', None), connection=conn)
                elif name == 'automation_webhook_setup':
                    secret = self.webhook_secret(conn, fresh, args.credential_request_id)
                    self.service.events.configure(args.automation_id,
                        WebhookSetup(revision=args.revision, provider=args.provider, secret=secret), actor, connection=conn)
                    row = dict(conn.execute('SELECT * FROM automations WHERE id=?', (args.automation_id,)).fetchone())
                else:
                    row = self.service.set_state(args.automation_id, Toggle(revision=args.revision, paused=name == 'automation_pause'),
                                                 actor, connection=conn)
                conn.execute('INSERT INTO automation_operations VALUES(?,?,?,?,?,?)', (*operation, digest, row['id'], row['revision']))
        await self.service.checkpoints.flush()
        result = (self.webhook_info(self.owned(row['id'], actor), actor) if name == 'automation_webhook_setup'
                  else await self.result(row, synchronize=name == 'automation_enable'))
        self.unchanged(run, actor)
        return result
