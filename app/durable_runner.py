"""Idempotent session steps. The database owns data; Temporal owns scheduling/retries.

Standalone mode retains one application process. Distributed mode uses one
coordinator and replicated workers with PostgreSQL leases and shared objects.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import time
from uuid import uuid4

import modal
from fastapi import HTTPException
from .environments import EnvironmentPending

from .runner import RunManager, SAVE_WARNING, TERMINAL, completed_response, safe_error_detail, refresh_sandbox_files, stop_requested
from .security import digest
from .sandboxes import ProvisioningTerminated
from .db import database
from sandbox.transport_recovery import MAX_TRANSPORT_ATTEMPTS, valid_retry


class LostExecution(Exception):
    pass


# Use one predicate for the partial index and its readers. Historical sessions
# and durable waits that released their machine must not add admission work.
from .database_schema import SQLITE_OCCUPIED_SESSION as OCCUPIED_SESSION, POSTGRES_OCCUPIED_SESSION


class DurableRunner(RunManager):
    def __init__(self, store, settings):
        super().__init__(store, settings)
        self.locks = {}
        from .runtime_coordination import AdmissionLock
        self.coordinated_database = store.database if settings.moyai_runtime_role != 'standalone' else None
        self.admission_lock = AdmissionLock(self.coordinated_database)
        self.occupied_session = POSTGRES_OCCUPIED_SESSION if store.database else OCCUPIED_SESSION
        store.execute("""CREATE TABLE IF NOT EXISTS durable_sessions (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0)""")
        store.execute(f"""CREATE INDEX IF NOT EXISTS idx_durable_sessions_occupied
            ON durable_sessions(run_id) WHERE {self.occupied_session}""")
        store.execute('''CREATE INDEX IF NOT EXISTS idx_durable_sessions_wake
            ON durable_sessions(run_id) WHERE revision>delivered''')
        from .prepared_sandboxes import PreparedSandboxes
        self.prepared = PreparedSandboxes(self)

    def state(self, run_id):
        rows = self.store.rows('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,))
        return json.loads(rows[0]['state']) if rows else {}

    def save(self, run_id, state):
        self.store.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id), write_scope=run_id)

    @asynccontextmanager
    async def session_guard(self, run_id, *, wait=True):
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        if not wait and lock.locked():
            yield False
            return
        async with lock:
            if self.coordinated_database:
                from .runtime_coordination import lease
                async with lease(self.coordinated_database, 'session:' + run_id, wait=wait) as acquired:
                    yield acquired
            else:
                yield True

    def submit_in(self, conn, run):
        if not run.get('deleted_at'):
            conn.execute('''INSERT INTO durable_sessions(run_id,revision) VALUES(?,1)
                ON CONFLICT(run_id) DO UPDATE SET revision=durable_sessions.revision+1''', (run['id'],))

    def submit(self, run):
        # The DB outbox owns dispatch, including a wake admitted with its state.
        with self.store.connect() as conn:
            self.submit_in(conn, run)

    async def wait_for_stop(self, run_id):
        # Temporal owns active turns, answers and warm/computer cleanup. Only
        # an idle journal has no owner left to settle orphaned input rows.
        while True:
            async with self.session_guard(run_id):
                if self.state(run_id).get('phase', 'idle') == 'idle':
                    await super().cleanup(self.store.run(run_id), run_id)
                    return
            await asyncio.sleep(0.2)

    def running_status(self, run_id, status, token_hash=None):
        # Never overwrite a stop that arrived during an awaited provider call.
        run = self.store.run(run_id)
        if stop_requested(run):
            return False
        fields = {'status': status}
        if token_hash is not None:
            fields['token_hash'] = token_hash
        from .db import now
        fields['updated_at'] = now()
        return bool(self.store.execute('UPDATE runs SET ' + ','.join(key + '=?' for key in fields) +
            " WHERE id=? AND status!='stopping' AND deletion_requested_at='' AND deleted_at=''",
            (*fields.values(), run_id), write_scope=run_id))

    def is_active(self, run_id):
        row = self.store.run(run_id)
        return bool(row and row['status'] not in TERMINAL)

    def token(self, run_id, message_id):
        # Reconstruct the same capability after a crash without putting it in
        # Temporal history or writing plaintext credentials to the database.
        secret = self.settings.encryption_key or self.settings.session_secret
        if not secret:
            raise ValueError('Durable execution requires a stable encryption/session key')
        return hmac.new(secret.encode(), f'moyai-turn-v1:{run_id}:{message_id}'.encode(), hashlib.sha256).hexdigest()

    def computer_wake_state(self, row, state, pending):
        """One lifecycle policy for presentation, retries, and atomic admission."""
        phase = state.get('phase', 'idle')
        action, starting, shutting_down, notice = 'blocked', False, False, ''
        if not row or row['deleted_at'] or row['mode'] != 'modal':
            action, notice = 'missing', 'Cloud session not found.'
            shutting_down = True
        elif (self.closing or stop_requested(row) or phase in {'cleanup', 'warm_cleanup'}
                or (phase == 'warm' and not state.get('computer_only')
                    and row['status'] in {'cancelled', 'interrupted'})):
            notice = 'This workspace is shutting down. Try again after it finishes.'
            shutting_down = True
        elif state.get('computer_only') and phase in {'provision', 'install', 'waiting_environment'}:
            action, notice = 'pending', 'Waking your workspace. Your live computer will appear here.'
        elif not state.get('computer_only') and phase in {'prepare', 'provision', 'install', 'launch', 'waiting_environment', 'startup_wait', 'transport_wait'}:
            starting = True
            notice = ('Preparing the workspace environment. The computer will appear when it is ready.'
                      if phase == 'waiting_environment' else
                      'Reconnecting workspace services. The computer will appear when they are ready.'
                      if phase in {'startup_wait', 'transport_wait'} else 'Starting your workspace. The computer will appear when it is ready.')
        elif phase in {'waiting_children', 'waiting_credential'}:
            notice = ('This session is waiting for its agents. The computer will return when work resumes.'
                      if phase == 'waiting_children' else
                      'This session is waiting for access. The computer will return when work resumes.')
        elif phase in {'save', 'checkpointed', 'finish'}:
            notice = 'This workspace is finishing and saving. Wait for it to finish before waking its computer.'
        elif pending or row['status'] not in TERMINAL:
            starting = row['status'] in {'queued', 'provisioning', 'reconnecting'} or (pending and phase in {'idle', 'warm'})
            notice = ('Your response is starting. The computer will appear when it is ready.' if starting else
                      'This session is active. Wait for it to finish before waking its computer.')
        elif phase not in {'idle', 'warm'}:
            notice = 'The workspace is busy. Wait for it to finish starting or stopping.'
        elif self.settings.missing_sandbox(row.get('sandbox_provider')):
            action, notice = 'unconfigured', 'Configure the session sandbox provider before waking its computer.'
        else:
            action = 'reuse' if phase == 'warm' else 'start'
        return {'can_wake': action in {'start', 'reuse'}, 'waking': action == 'pending',
                'starting': starting, 'shutting_down': shutting_down, 'wake_notice': notice, 'wake_action': action,
                'wake_error': state.get('computer_error', '')}

    def computer_state(self, run_id):
        with self.store.connect() as conn:
            conn.begin_read()
            row = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            state = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,)).fetchone()
            pending = conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running','injected')", (run_id,)).fetchone()
            return self.computer_wake_state(dict(row) if row else None,
                json.loads(state['state']) if state else {}, bool(pending))

    async def wake_computer(self, run_id):
        async with self.session_guard(run_id):
            policy = self.computer_state(run_id)
            if policy['waking']:
                return  # A retry observes the already persisted wake operation.
            if not policy['can_wake']:
                raise HTTPException({'missing': 404, 'unconfigured': 503}.get(policy['wake_action'], 409), policy['wake_notice'])
            if policy['wake_action'] == 'reuse':
                try:
                    await self.sandbox(self.state(run_id))
                    return
                except LostExecution:
                    await self.release_warm(run_id, self.state(run_id), 'machine no longer available')
            async with self.admission(run_id) as available:
                if not available:
                    raise HTTPException(409, 'All workspaces are busy. Try waking this computer again shortly.')
                # Delete and enqueue use the same write transaction boundary.
                # Persist admission and its dispatch together before effects.
                with self.store.connect() as conn:
                    conn.begin_write()
                    row = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                    row = dict(row) if row else None
                    current = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,)).fetchone()
                    pending = conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running','injected')", (run_id,)).fetchone()
                    policy = self.computer_wake_state(row, json.loads(current['state']) if current else {}, bool(pending))
                    if policy['wake_action'] != 'start':
                        raise HTTPException({'missing': 404, 'unconfigured': 503}.get(policy['wake_action'], 409),
                                            policy['wake_notice'] or 'The workspace changed. Refresh before waking it again.')
                    state = {'version': 1, 'phase': 'provision', 'computer_only': True,
                             'message_id': 'computer-' + uuid4().hex, 'segment': 0,
                             'snapshot_id': row['snapshot_id'], 'sandbox_id': ''}
                    self.submit_in(conn, row)
                    conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
            await self.persist()

    async def advance_computer(self, run_id, state):
        run = await database(self.store.run, run_id)
        if stop_requested(run) or state['phase'] == 'warm_cleanup':
            await self.release_warm(run_id, state, 'computer stopped')
            if (await database(self.store.run, run_id))['status'] == 'stopping':
                await database(self.store.update_run, run_id, status='cancelled')
            return await database(self.store.has_queued_messages, run_id)
        try:
            if state['phase'] in {'provision', 'waiting_environment'}:
                result = await self.provision(run_id, state)
                return 'capacity' if result == 'capacity' else True
            sandbox = await self.sandbox(state)
            await refresh_sandbox_files(sandbox)
            await self.computer.restore(sandbox, run_id)
            result = await self.computer.wake(sandbox)
            if result.get('error') or not result.get('available'):
                raise HTTPException(503, 'Computer could not start.')
            state.update(phase='warm', idle_until=time.time() + self.computer_idle_seconds(state))
            await database(self.save, run_id, state)
            return True
        except EnvironmentPending as pending:
            state.update(phase='waiting_environment', environment_build=pending.build_id)
            await database(self.save, run_id, state)
            return {'retry_seconds': 5}
        except (LostExecution, ValueError, HTTPException, modal.exception.ImageBuildError, modal.exception.InvalidError):
            # No synthetic message, answer, model invocation or chat failure.
            state.update(phase='warm_cleanup', computer_error='The computer could not start. Try waking it again.')
            await database(self.save, run_id, state)
            return True

    async def advance(self, run_id):
        async with self.session_guard(run_id):
            if self.credentials and hasattr(self.credentials, 'reconcile_resolutions'):
                await database(self.credentials.reconcile_resolutions, run_id)
            row = await database(self.store.run, run_id)
            if not row or row['deleted_at']:
                return False
            state = await database(self.state, run_id)
            # Stop only at boundaries before a process is launched. Active
            # execution, checkpoint saving and cleanup keep running. The saved
            # state and inbox are resumed by the replacement worker.
            if (self.settings.maintenance_drain and row['status'] != 'stopping'
                    and state.get('phase', 'idle') in {
                        'idle', 'warm', 'prepare', 'provision', 'waiting_environment',
                        'install', 'startup_wait', 'transport_wait',
                        'waiting_children', 'waiting_credential'}):
                return {'retry_seconds': 15}
            if state.get('computer_only') and state.get('phase') not in {'idle', 'warm'}:
                return await self.advance_computer(run_id, state)
            if state.get('phase') in {'warm', 'warm_cleanup'}:
                reuse = await self.warm(run_id, state)
                if reuse is not True:
                    return reuse
                row = await database(self.store.run, run_id)
                state = await database(self.state, run_id)
                if state.get('phase') == 'warm':
                    # A queued follow-up claims the already occupied slot.
                    if not await database(self.begin_turn, run_id, row, state):
                        return True
                    state = await database(self.state, run_id)
            if not state or state.get('phase') == 'idle':
                # Only an idle journal has relinquished turn ownership. Repair
                # old terminal inputs without interrupting a fresh queued turn.
                for answer in (await database(self.store.rows, """SELECT a.response_to_id,a.content,m.status FROM messages a
                        JOIN messages m ON m.id=a.response_to_id AND m.run_id=a.run_id
                        WHERE a.run_id=? AND a.status='saving' AND m.role='user'
                        AND m.status IN ('completed','failed','cancelled','interrupted','steered','save_failed')""", (run_id,))):
                    await database(self.store.finish_message, run_id, answer['response_to_id'], answer['content'], answer['status'])
                if stop_requested(row) or row['status'] in {'cancelled', 'interrupted'}:
                    if row['status'] == 'stopping':
                        await database(self.store.update_run, run_id, status='cancelled')
                    return False
                if not await database(self.store.has_queued_messages, run_id) and not await database(self.store.rows,
                        "SELECT id FROM messages WHERE run_id=? AND status='running'", (run_id,)):
                    return False
                async with self.admission(run_id, prepared=True) as available:
                    if not available:
                        return 'capacity'
                    if not await database(self.begin_turn, run_id, await database(self.store.run, run_id)):
                        return True
                    state = await database(self.state, run_id)
                if state.get('phase') == 'idle' or not state:
                    return False
            # Cancellation is persistent, including across worker restarts.
            if stop_requested(await database(self.store.run, run_id)) and state['phase'] not in {'cleanup', 'finish'}:
                await database(self.fail, run_id, state, 'The response was stopped. The last saved workspace is preserved.', 'cancelled')
            try:
                return await self.step(run_id, state)
            except EnvironmentPending as pending:
                if state.get('environment_build') != pending.build_id:
                    await database(self.store.event, run_id, 'status', str(pending), {'activity_version': 1, 'phase': 'environment'})
                state.update(phase='waiting_environment', environment_build=pending.build_id)
                await database(self.save, run_id, state)
                return {'retry_seconds': 5}
            except LostExecution:
                if ((state.get('reused_machine') and state['phase'] in {'prepare', 'install'})
                        or (state.get('startup_attempt') and state['phase'] == 'install'
                            and not state.get('execution_started'))
                        or (state.get('resume_transport') and state['phase'] == 'install'
                            and not state.get('execution_started'))):
                    # This new turn has not reached its launch boundary yet.
                    state.update(phase='provision', sandbox_id='', reused_machine=False)
                    await database(self.save, run_id, state)
                    return True
                await database(self.fail, run_id, state,
                          'The cloud process stopped before its work could be confirmed. The last checkpoint is preserved. '
                          'No unfinished actions were replayed; send a new message to inspect and continue.', 'interrupted')
                return True
            except ValueError:
                await database(self.fail, run_id, state, 'This response cannot run with the current workspace configuration. '
                          'An administrator must check the selected model and runtime settings.')
                return True
            except modal.exception.ImageBuildError:
                # Retrying the same broken image leaves the UI provisioning
                # forever. Finish this turn without exposing provider logs.
                await database(self.fail, run_id, state, 'The workspace image could not be built. '
                          'An administrator must fix the image build and redeploy before you retry.')
                return True
            except HTTPException as exc:
                # A disabled/deleted selection is a configuration failure, not
                # a transient provider error for Temporal to retry indefinitely.
                await database(self.fail, run_id, state, str(exc.detail))
                return True

    def begin_turn(self, run_id, row, warm=None):
        if stop_requested(row) or row['status'] in {'cancelled', 'interrupted'}:
            return False
        messages = self.store.rows("SELECT * FROM messages WHERE run_id=? AND status='running'", (run_id,))
        message = messages[0] if messages else self.store.claim_message(run_id)
        if not message:
            return False
        state = {'version': 1, 'phase': 'prepare', 'message_id': message['id'], 'segment': 0,
                 'snapshot_id': row['snapshot_id'], 'sandbox_id': '', 'cursor': 0, 'turn_started': time.time()}
        if warm and warm.get('sandbox_id'):
            state.update(sandbox_id=warm['sandbox_id'], machine_started=warm['machine_started'], reused_machine=True)
            self.store.event(run_id, 'status', 'Reusing the saved session sandbox for this response.')
        elif self.prepared.assign(run_id, row, state):
            return True
        elif not self.has_capacity():
            # A prepared entry may expire after admission's optimistic check.
            # Keep the claimed input durable, but do not create a cold-machine
            # reservation until ordinary capacity is available.
            return False
        self.save(run_id, state)
        return True

    def rotation_seconds(self, state):
        from .sandboxes import provider_for_id
        return self.settings.sandbox_rotation_for(provider_for_id(state.get('sandbox_id', '')))

    def idle_deadline(self, state):
        # A follow-up never extends the machine's absolute renewal deadline.
        return min(state.get('idle_until') or float('inf'),
                   state['machine_started'] + self.rotation_seconds(state))

    def computer_idle_seconds(self, state):
        return self.settings.sandbox_idle_seconds or (300 if state.get('computer_only') else 0)

    async def release_warm(self, run_id, state, reason):
        # Persist before the provider call: a lost termination ACK is retryable
        # and never publishes the previous answer a second time.
        state.update(phase='warm_cleanup', idle_reason=reason)
        await database(self.save, run_id, state)
        await self.cleanup(state, run_id)
        state.update(phase='idle', sandbox_id='')
        state.pop('idle_until', None)
        await database(self.save, run_id, state)
        await database(self.store.update_run, run_id, sandbox_id='', token_hash='')
        await database(self.store.event, run_id, 'status', 'Idle sandbox released: ' + reason + '.')

    async def warm(self, run_id, state):
        row = await database(self.store.run, run_id)
        if getattr(self, 'computer', None) and state.get('phase') == 'warm':
            touched = self.computer.touched(run_id)
            if touched and state.get('idle_until') is not None:
                state['idle_until'] = max(state['idle_until'], touched + self.computer_idle_seconds(state))
                await database(self.save, run_id, state)
        stopping = stop_requested(row) or (not state.get('computer_only') and row['status'] in {'cancelled', 'interrupted'})
        queued = await database(self.store.has_queued_messages, run_id)
        expired = (time.time() >= state['machine_started'] + self.rotation_seconds(state)
                   or (not queued and time.time() >= self.idle_deadline(state)))
        if (state['phase'] == 'warm_cleanup' or stopping or not self.computer_idle_seconds(state)
                or expired):
            await self.release_warm(run_id, state, state.get('idle_reason', 'idle timeout' if not stopping else 'stop requested'))
            row = await database(self.store.run, run_id)
            stopping = stop_requested(row) or row['status'] in {'cancelled', 'interrupted'}
            if row['status'] == 'stopping':
                await database(self.store.update_run, run_id, status='cancelled')
            return False if stopping else (await database(self.store.has_queued_messages, run_id))
        if not queued:
            if state.get('idle_until') is None:
                state['idle_until'] = time.time() + self.computer_idle_seconds(state)
                await database(self.save, run_id, state)
            return {'idle_seconds': max(0.01, self.idle_deadline(state) - time.time())}
        state['idle_until'] = None
        await database(self.save, run_id, state)
        try:
            await self.sandbox(state)
        except LostExecution:
            await self.release_warm(run_id, state, 'machine no longer available')
            return True
        return True

    @asynccontextmanager
    async def admission(self, run_id, *, prepared=False):
        # Hold global admission only for the capacity check and the caller's
        # persisted reservation. Provider cleanup keeps its session lease and
        # occupied slot, but cannot prevent other sessions from claiming space.
        while True:
            async with self.admission_lock:
                if await database(self.has_capacity, run_id if prepared else None):
                    yield True
                    return
            if not await self.make_capacity(run_id, prepared=prepared):
                yield False
                return
            # Reclamation is not a reservation: another worker may have claimed
            # the freed slot. Check again under global admission before yielding.

    async def make_capacity(self, run_id, *, prepared=False):
        # Best-effort reclamation outside global admission. The caller must use
        # admission() to reserve the space. Skip owned sessions to avoid cycles
        # between admissions that each already hold their own session lease.
        if await database(self.has_capacity, run_id if prepared else None):
            return True
        if await self.prepared.reclaim():
            return True
        candidates = [(r['run_id'], json.loads(r['state'])) for r in await database(self.store.rows, f"""
            SELECT run_id,state FROM durable_sessions WHERE {self.occupied_session}
            AND json_text(state, 'phase') IN ('warm', 'warm_cleanup') ORDER BY run_id""")]
        for other, state in sorted(candidates, key=lambda entry: entry[1].get('idle_until') or float('inf')):
            lock = self.locks.setdefault(other, asyncio.Lock())
            if other == run_id or lock.locked() or state.get('phase') not in {'warm', 'warm_cleanup'}:
                continue
            async with self.session_guard(other, wait=False) as acquired:
                if not acquired:
                    continue
                if await database(self.has_capacity, run_id if prepared else None):
                    return True
                state = await database(self.state, other)
                if state.get('phase') not in {'warm', 'warm_cleanup'}:
                    continue
                if await database(self.store.has_queued_messages, other):
                    continue
                await self.release_warm(other, state, 'capacity needed by another session')
                if await database(self.has_capacity, run_id if prepared else None):
                    return True
        return False

    def has_capacity(self, run_id=None):
        # We only need to know whether the limit is reached. Ordered, bounded
        # index reads also avoid Postgres overestimating JSON predicate matches
        # and choosing a scan of retained history for an unbounded count.
        active = self.store.rows(f"""SELECT COUNT(*) AS occupied
            FROM (SELECT run_id FROM durable_sessions WHERE {self.occupied_session}
                ORDER BY run_id LIMIT ?) AS slots""", (self.settings.max_concurrent_runs,))[0]['occupied']
        if active >= self.settings.max_concurrent_runs:
            return False
        return (active + self.prepared.count() < self.settings.max_concurrent_runs
                or bool(run_id and self.prepared.ready(run_id)))

    async def snapshot_for_children(self, run):
        sandbox = await self.sandbox(self.state(run['id']))
        snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds, ttl=None)
        return snapshot.object_id

    def fail(self, run_id, state, explanation, status='failed'):
        if state['phase'] == 'provision' and not state.get('sandbox_id'):
            # Recover provision journals written before sandbox_name existed.
            state.setdefault('sandbox_name', f"moyai-{run_id}-{state['message_id']}-{state['segment']}")
        preserved = self.preserve_answer(run_id)
        state.update(phase='cleanup', outcome='save_failed' if preserved else status,
                     response=self.store.run(run_id)['summary'] if preserved else explanation)
        self.store.update_run(run_id, token_hash='', error=explanation)
        self.save(run_id, state)
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))

    async def sandbox(self, state):
        try:
            sandbox = await self.provider(identity=state['sandbox_id']).get(state['sandbox_id'])
        except modal.exception.NotFoundError:
            raise LostExecution() from None
        if await sandbox.poll.aio() is not None:
            raise LostExecution()
        return sandbox

    def provision_name(self, run_id, state):
        suffix = f"-provision-{state['provision_attempt']}" if state.get('provision_attempt') else ''
        return f"moyai-{run_id}-{state['message_id']}-{state['segment']}{suffix}"

    async def provision(self, run_id, state):
        project = await self.environments.prepare(run_id) if self.environments else {}
        if stop_requested((await database(self.store.run, run_id))):
            return True
        if state['phase'] == 'waiting_environment':
            async with self.admission(run_id) as available:
                if not available:
                    return 'capacity'
                if stop_requested((await database(self.store.run, run_id))):
                    return True
                state['phase'] = 'provision'
                await database(self.save, run_id, state)
        backend = self.provider((await database(self.store.run, run_id)))
        name = self.provision_name(run_id, state)
        state['sandbox_name'] = name
        await database(self.save, run_id, state)  # Keep create identity even if its ACK is lost.
        token = '' if state.get('computer_only') else self.token(run_id, state['message_id'])
        try:
            try:
                sandbox = await backend.find(name, initialize=True, token=token)
            except modal.exception.NotFoundError:
                try:
                    sandbox = await backend.create(name=name, snapshot_id=state['snapshot_id'] or project.get('snapshot_id') or '',
                                                   token=token)
                except modal.exception.AlreadyExistsError:
                    sandbox = await backend.find(name, initialize=True, token=token)
        except ProvisioningTerminated:
            # Provisioning has not crossed the agent launch boundary. A new
            # durable name is safe only after the provider confirms the old VM
            # cannot run, and must survive worker restarts without duplication.
            attempt = state.get('provision_attempt', 0)
            if attempt >= 2:
                if state.get('computer_only'):
                    raise HTTPException(503, 'Computer startup failed repeatedly.')
                await database(self.fail, run_id, state, 'Workspace startup failed repeatedly. The last checkpoint is preserved; '
                          'no agent work was replayed. Try again when workspace services recover.')
            else:
                state['provision_attempt'] = attempt + 1
                await database(self.save, run_id, state)
                await database(self.store.event, run_id, 'status', 'Startup VM stopped. Retrying from the saved workspace.',
                                 {'phase': 'provision', 'attempt': attempt + 1})
            return True
        state.update(sandbox_id=sandbox.object_id, machine_started=getattr(sandbox, 'started_at', time.time()), phase='install')
        await database(self.save_provisioned, run_id, state)
        return sandbox

    def save_provisioned(self, run_id, state):
        # Cancellation may detach the worker immediately after this commit.
        # The resumed install and workspace APIs must agree about its machine.
        with self.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
            conn.execute('UPDATE runs SET sandbox_id=? WHERE id=?', (state['sandbox_id'], run_id))

    def directory(self, state):
        suffix = f"-startup-{state['startup_attempt']}" if state.get('startup_attempt') else ''
        return f"/session/executions/{state['message_id']}-{state['segment']}{suffix}"

    def startup_retry_allowed(self, run, state):
        marker = state.get('result', {}).get('startup_retry')
        return (isinstance(marker, dict) and marker.get('version') == 1
                and marker.get('stage') in {'workspace_tools', 'attachments', 'repository_metadata', 'context_window'}
                and marker.get('reason') in {'network', 'HTTP 408', 'HTTP 425', 'HTTP 429',
                                             'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504'}
                and state.get('exit_code') == 75 and not state.get('execution_started')
                and 'startup_model_calls' in state
                and run['turn_model_calls'] == state['startup_model_calls'])

    def wait_for_startup(self, run_id, state):
        state.setdefault('startup_deadline', time.time() + self.settings.startup_recovery_seconds)
        if time.time() >= state['startup_deadline']:
            self.fail(run_id, state, 'Workspace services did not reconnect after repeated startup attempts. '
                      'No new agent work was started. Your request and files are saved; try again when services recover.')
            return
        state['startup_attempt'] = state.get('startup_attempt', 0) + 1
        state.update(phase='startup_wait', retry_at=min(state['startup_deadline'],
                     time.time() + min(30, 2 ** min(state['startup_attempt'], 5))))
        self.running_status(run_id, 'reconnecting')
        self.store.update_run(run_id, summary='', pending_result='')
        self.store.event(run_id, 'status', 'Reconnecting to workspace services. This request will resume automatically.',
                         {'activity_version': 1, 'phase': 'reconnecting', 'attempt': state['startup_attempt'],
                          **state['result']['startup_retry']})
        self.save(run_id, state)

    def wait_for_transport(self, run_id, state):
        # The process exited and its exact public receipts/files were saved.
        # This counter belongs to the original user turn, not an SDK segment.
        attempt = state.get('transport_attempt', 0) + 1
        if attempt > MAX_TRANSPORT_ATTEMPTS:
            self.fail(run_id, state, 'The cloud connection did not recover after three continuation attempts. '
                      'Your request, completed tool receipts and error diagnostics are saved. '
                      'No completed tool calls were automatically replayed.')
            return
        state.update(phase='transport_wait', transport_attempt=attempt, retry_at=time.time() + 2 ** attempt)
        self.save(run_id, state)
        self.running_status(run_id, 'reconnecting', '')
        self.store.update_run(run_id, summary='', pending_result='')
        self.store.event(run_id, 'status', 'Workspace saved. Reconnecting to continue from completed tool receipts.',
                         {'activity_version': 1, 'phase': 'reconnecting', 'attempt': attempt,
                          'request_id': state['result']['transport_retry']['failure'].get('request_id')})

    async def command(self, sandbox, *args, token=None):
        # Pass capabilities only in the exec environment, never in a persisted
        # task spec, command argument or Temporal payload. Reused machines have
        # an older creation-time environment, so every launch overrides it.
        process = await sandbox.exec.aio('/usr/local/bin/python', '/opt/workspace-runner/durable_process.py', *args,
                                         timeout=30, env=self.settings.broker_environment(token) if token else {})
        output, _ = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
        if await process.wait.aio() != 0:
            raise RuntimeError('Sandbox supervisor command failed')
        return output

    def guard_agent_completion(self, run_id, result):
        # A native final ends one model segment, not outstanding delegation.
        # Query persisted groups: steering can change turns, and workers may
        # settle after this segment received partial results. Only handoff (or
        # explicit cancellation) discharges the obligation to gather results.
        if self.coordinator and completed_response(self.store.run(run_id), result):
            group = self.coordinator.pending_group(run_id)
            if group:
                return {**result, 'completed': False, 'continuation': True, 'wait_group': group}
        return result

    def record_report(self, run_id, state, report):
        # Persist the complete received batch before cancellation detaches this
        # worker. database() shields the operation and retains its session lease.
        for event in report['events']:
            if event.get('kind') == 'trace' and self.store.tracing:
                self.store.tracing.tool(run_id, event.get('data'))
            if event.get('kind') in {'tool', 'status', 'error', 'message'}:
                self.store.event(run_id, event['kind'], str(event.get('message', '')), event.get('data', {}))
            event_phase = event.get('data', {}).get('phase')
            if event_phase == 'execution_started':
                state['execution_started'] = True
                self.acknowledge_credential(run_id, state)
                state.pop('startup_deadline', None)
                self.running_status(run_id, 'running')
            elif (event.get('kind') == 'status' and event.get('data', {}).get('stage') == 'model_transport'
                    and state.get('execution_started') and event_phase in {'reconnecting', 'recovered'}):
                self.running_status(run_id, 'running' if event_phase == 'recovered' else 'reconnecting')
            elif event_phase == 'reconnecting' and not state.get('execution_started'):
                self.running_status(run_id, 'reconnecting')
            if event.get('kind') == 'error':
                state['last_error'] = str(event.get('message', ''))
        state['cursor'] = report['cursor']
        if report.get('final'):
            # sandbox.agent can emit a completed/steered final only
            # after resumed_context and execution_started. The final
            # also acknowledges delivery if that event batch was lost.
            if report['final'].get('completed') or report['final'].get('steer_message_id'):
                self.acknowledge_credential(run_id, state)
            self.message_queue.acknowledge(run_id, state['message_id'], report['final'].get('steering_applied', []))
            state['result'] = self.guard_agent_completion(
                run_id, {**report['final'], 'message_id': state['message_id']})
            attempts = report['final'].get('transport_attempt')
            if type(attempts) is int and attempts >= 0:
                state['transport_attempt'] = max(state.get('transport_attempt', 0),
                                                 min(attempts, MAX_TRANSPORT_ATTEMPTS))
            if not state['result'].get('startup_retry') and not state['result'].get('transport_retry'):
                self.receive_result(run_id, state['result'])
        self.save(run_id, state)

    async def step(self, run_id, state):
        phase = state['phase']
        run = await database(self.store.run, run_id)
        if phase in {'waiting_children', 'waiting_credential'}:
            control = self.message_queue.live_control(run_id, state['message_id'], [], checkpointed=True)
            if control.get('handoff'):
                # A different requester/model still needs a capability boundary.
                # The checkpoint is already durable; no synthetic chat answer.
                state.update(phase='finish', outcome='steered', response='', keep_warm=False)
                await database(self.save, run_id, state)
                return True
            if control.get('input'):
                async with self.admission(run_id) as available:
                    if not available:
                        return 'capacity'
                    if stop_requested((await database(self.store.run, run_id))):
                        return True
                    # Resume the saved conversation under the same turn and scope.
                    # Leave the input pending until the restored agent acknowledges
                    # native delivery; a worker restart cannot lose the correction.
                    state.update(phase='provision', resume_group=state.get('wait_group'),
                                 resume_credential=state.get('wait_credential'))
                    await database(self.save, run_id, state)
                await database(self.running_status, run_id, 'provisioning')
                return True
        if phase == 'prepare':
            if self.prepare_context:
                await self.prepare_context(run_id)
            if stop_requested((await database(self.store.run, run_id))):
                return True
            if run['mode'] == 'demo':
                await self.demo(run)
                if stop_requested((await database(self.store.run, run_id))):
                    await database(self.fail, run_id, state, 'The response was stopped.', 'cancelled')
                    return True
                row = await database(self.store.run, run_id)
                state.update(phase='finish', outcome='steered' if row['status']=='steered' else 'completed', response=row['summary'])
            else:
                state['phase'] = 'install' if state.get('sandbox_id') else 'provision'
                await database(self.running_status, run_id, 'running' if state.get('sandbox_id') else 'provisioning')
            await database(self.save, run_id, state)
        elif phase in {'provision', 'waiting_environment'}:
            result = await self.provision(run_id, state)
            if result == 'capacity':
                return 'capacity'
        elif phase == 'install':
            sandbox = await self.sandbox(state)

            async def prepare_files():
                # A clean pool entry already has this exact runtime. Snapshots
                # and entries claimed before an upgrade still need refreshing.
                if state.get('prepared_build') != self.prepared.build:
                    await refresh_sandbox_files(sandbox)
                if getattr(self, 'computer', None):
                    await self.computer.restore(sandbox, run_id, required=False)

            def prepare_spec():
                message = self.store.rows('SELECT content FROM messages WHERE id=?', (state['message_id'],))[0]
                return self.spec({**run, 'prompt': message['content'], 'message_id': state['message_id'],
                                  'continuation': state['segment'] > 0})

            # Independent provider I/O and database reads overlap. Neither can
            # launch the agent; both must finish before writing the launch spec.
            preparation = [asyncio.create_task(prepare_files()), asyncio.create_task(database(prepare_spec))]
            try:
                _, spec = await asyncio.gather(*preparation)
            finally:
                # Keep the original exception type for advance's permanent-error
                # handling, and finish cancellation before releasing ownership.
                for task in preparation:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*preparation, return_exceptions=True)
            state.pop('prepared_build', None)
            spec['rotation_seconds'] = self.settings.temporal_checkpoint_seconds
            if run.get('sandbox_provider') == 'lambda':
                spec['rotation_at'] = state['machine_started'] + self.rotation_seconds(state)
            spec['chat_enabled'] = True
            if (run['harness'] == 'codex' and self.settings.codex_runtime_reuse
                    and self.settings.sandbox_idle_seconds and not run['parent_run_id']):
                spec['codex_runtime_idle_seconds'] = min(300, self.settings.sandbox_idle_seconds)
                spec['codex_runtime_scope'] = [run_id, run['active_user_id'], spec['model']]
            spec['transport_attempt'] = state.get('transport_attempt', 0)
            if state.get('resume_transport'):
                spec['transport_recovery'] = state['resume_transport']
            if state.get('resume_group') and self.coordinator:
                spec['agent_results'] = await database(self.coordinator.results, run_id, state['resume_group'])
            state.pop('credential_delivery', None)
            if state.get('resume_credential') and self.credentials:
                spec['credential_resolution'] = await database(self.credentials.resolution, run_id,state['resume_credential'])
                resolution = spec['credential_resolution']
                if resolution['status'] in {'provided', 'declined', 'satisfied'} and 'generation' in resolution:
                    state['credential_delivery'] = {'request_id': state['resume_credential'],
                                                    'generation': resolution['generation']}
            if self.settings.run_timeout_seconds:
                remaining = self.settings.run_timeout_seconds - (time.time() - state['turn_started'])
                if remaining <= 0:
                    await database(self.fail, run_id, state, 'This response reached its configured time limit.')
                    return True
                spec['timeout'] = remaining
            # Each piece gets its own immutable input and launch marker.
            spec_path = '/tmp/moyai-' + self.directory(state).rsplit('/', 1)[-1] + '.json'
            await sandbox.filesystem.write_text.aio(json.dumps(spec), spec_path)
            state.update(phase='launch', startup_model_calls=(await database(self.store.run, run_id))['turn_model_calls'],
                         execution_started=False)
            await database(self.save, run_id, state)
        elif phase == 'launch':
            sandbox = await self.sandbox(state)
            if not (await database(self.running_status, run_id, 'running', digest(self.token(run_id, state['message_id'])))):
                return True
            spec_path = '/tmp/moyai-' + self.directory(state).rsplit('/', 1)[-1] + '.json'
            await self.command(sandbox, 'start', self.directory(state), spec_path, token=self.token(run_id, state['message_id']))
            state['phase'] = 'monitor'
            await database(self.save, run_id, state)
        elif phase == 'monitor':
            sandbox = await self.sandbox(state)
            # One Activity may poll multiple times, keeping workflow history
            # small. Heartbeats are delivered independently by the worker.
            for _ in range(10):
                if stop_requested((await database(self.store.run, run_id))):
                    return True
                output = await self.command(sandbox, 'read', self.directory(state), str(state['cursor']))
                for value in (self.token(run_id, state['message_id']), self.settings.litellm_api_key, self.settings.modal_token_secret,
                              self.settings.cloudflare_access_client_id, self.settings.cloudflare_access_client_secret):
                    if value:
                        output = output.replace(value, '[redacted]')
                report = json.loads(output)
                await database(self.record_report, run_id, state, report)
                if report['state'] == 'uncertain':
                    raise LostExecution()
                if report['state'] == 'new':
                    # A launch RPC may have succeeded without its process
                    # starting yet. The supervisor safely arbitrates retries.
                    state['phase'] = 'launch'
                    await database(self.save, run_id, state)
                    return True
                if report['state'] == 'done' and len(report['events']) < 30:
                    state['exit_code'] = report['exit_code']
                    if self.startup_retry_allowed((await database(self.store.run, run_id)), state):
                        await database(self.wait_for_startup, run_id, state)
                        return True
                    if state.get('result', {}).get('startup_retry'):
                        await database(self.fail, run_id, state, 'The agent stopped, but a safe startup retry could not be confirmed. '
                                  'No potentially completed work was replayed.')
                        return True
                    state['phase'] = 'save'
                    await database(self.save, run_id, state)
                    return True
                await asyncio.sleep(2)
        elif phase == 'startup_wait':
            if time.time() >= state['startup_deadline']:
                await database(self.wait_for_startup, run_id, state)
                return True
            remaining = state['retry_at'] - time.time()
            if remaining > 0:
                return {'retry_seconds': remaining}
            state.update(phase='install', cursor=0)
            for key in ('result', 'exit_code', 'last_error', 'save_attempts'):
                state.pop(key, None)
            await database(self.save, run_id, state)
        elif phase == 'transport_wait':
            await database(self.running_status, run_id, 'reconnecting', '')
            if (self.settings.run_timeout_seconds and
                    time.time() - state['turn_started'] >= self.settings.run_timeout_seconds):
                await database(self.fail, run_id, state, 'This response reached its configured time limit while reconnecting. '
                          'Completed work and error diagnostics are saved.')
                return True
            remaining = state['retry_at'] - time.time()
            if remaining > 0:
                return {'retry_seconds': remaining}
            if time.time() >= state['machine_started'] + self.rotation_seconds(state):
                await self.cleanup(state, run_id)
                state['sandbox_id'] = ''
            state.update(phase='install' if state.get('sandbox_id') else 'provision',
                         segment=state['segment'] + 1, cursor=0, execution_started=False,
                         resume_transport=state['result']['transport_retry'])
            for key in ('result', 'exit_code', 'last_error', 'save_attempts', 'startup_attempt', 'startup_deadline'):
                state.pop(key, None)
            await database(self.save, run_id, state)
        elif phase == 'save':
            sandbox = await self.sandbox(state)
            result = state.get('result', {})
            # Answer persisted above before archive/snapshot work.
            await self.save_artifact(sandbox, run_id)
            if not (await database(self.running_status, run_id, 'saving', '')):
                return True
            try:
                snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds, ttl=None)
            except Exception as exc:
                state['save_attempts'] = state.get('save_attempts', 0) + 1
                await database(self.save, run_id, state)
                await database(self.store.event, run_id, 'error', 'Workspace checkpoint could not be confirmed.',
                                 {'stage': 'snapshot_filesystem', 'attempt': state['save_attempts'],
                                  'reason': safe_error_detail(exc, [self.settings.modal_token_secret])})
                if state['save_attempts'] >= 3:
                    await database(self.fail, run_id, state, SAVE_WARNING if result else 'Workspace saving failed. No unfinished work was replayed.')
                    return True
                raise
            result.update(checkpoint_saved=True, exit_code=state['exit_code'])
            state.update(snapshot_id=snapshot.object_id, phase='checkpointed', result=result)
            # Commit checkpoint and protocol phase together so a crash cannot
            # roll a turn forward without the matching conversation snapshot.
            with self.store.connect() as conn:
                conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
                conn.execute("UPDATE runs SET snapshot_id=?,checkpoint_error='',pending_result=? WHERE id=?",
                             (snapshot.object_id, '' if result.get('transport_retry') else json.dumps(result), run_id))
        elif phase == 'checkpointed':
            # Also cover receipts checkpointed before this safeguard existed.
            result = await database(self.guard_agent_completion, run_id, state['result'])
            if result != state['result']:
                state['result'] = result
                await database(self.save, run_id, state)
                await database(self.receive_result, run_id, result)
            if result.get('transport_retry'):
                if state['exit_code'] == 75 and valid_retry(result['transport_retry']) and not result.get('completed'):
                    self.wait_for_transport(run_id, state)
                else:
                    await database(self.fail, run_id, state, 'A safe cloud recovery could not be confirmed. '
                              'Saved receipts and error diagnostics are preserved; no actions were replayed.')
                return True
            # This segment consumed the old transport checkpoint and saved a
            # newer one. Child/credential handoffs must resume that new state,
            # without asking the harness to verify the obsolete recovery marker.
            state.pop('resume_transport', None)
            continuing = result.get('continuation') and state['exit_code'] == 0
            steered = (state['exit_code'] == 0 and
                       self.message_queue.accepted(run_id, result.get('steer_message_id')))
            if steered:
                keep = (self.settings.sandbox_idle_seconds and not run['parent_run_id']
                        and time.time() < state['machine_started'] + self.rotation_seconds(state))
                state.update(phase='finish' if keep else 'cleanup', keep_warm=bool(keep), outcome='steered', response='')
                await database(self.save, run_id, state)
            elif continuing and result.get('wait_credential') and self.credentials:
                await database(self.credentials.resolution, run_id,result['wait_credential'])
                await self.cleanup(state, run_id)
                state.update(phase='waiting_credential',wait_credential=result['wait_credential'],sandbox_id='',
                             segment=state['segment']+1,cursor=0)
                state.pop('result',None)
                state.pop('save_attempts',None)
                await database(self.running_status, run_id,'waiting_credential','')
                await database(self.store.update_run, run_id,pending_result='',summary='',sandbox_id='')
                await database(self.store.event, run_id,'credential','Workspace saved. Waiting for access; sandbox released.',
                                 {'request_id':state['wait_credential']})
                await database(self.save, run_id,state)
            elif continuing and result.get('wait_group') and self.coordinator:
                # Validate the control message against persisted ownership.
                self.coordinator.group(run_id, result['wait_group'])
                await self.cleanup(state, run_id)
                state.update(phase='waiting_children', wait_group=result['wait_group'], sandbox_id='',
                             segment=state['segment'] + 1, cursor=0)
                state.pop('result', None)
                state.pop('save_attempts', None)
                await database(self.running_status, run_id, 'waiting_children', '')
                await database(self.store.update_run, run_id, pending_result='', summary='')
                await database(self.store.event, run_id, 'agents', 'Workspace saved. Waiting for parallel agents; coordinator sandbox released.',
                                 {'group_id': state['wait_group']})
                await database(self.save, run_id, state)
            elif continuing:
                if time.time() - state['machine_started'] >= self.rotation_seconds(state):
                    await self.cleanup(state, run_id)
                    state.update(sandbox_id='', phase='provision')
                else:
                    state['phase'] = 'install'
                state.update(segment=state['segment'] + 1, cursor=0)
                state.pop('result', None)
                state.pop('save_attempts', None)
                await database(self.running_status, run_id, 'running')
                await database(self.store.update_run, run_id, pending_result='', summary='')
                await database(self.store.event, run_id, 'status', 'Workspace checkpoint saved. Continuing the same request.')
                await database(self.save, run_id, state)
            else:
                completed = result.get('completed') and state['exit_code'] == 0
                keep = (completed and self.settings.sandbox_idle_seconds and run['chat_enabled'] and not run['parent_run_id']
                        and time.time() < state['machine_started'] + self.rotation_seconds(state))
                state.update(phase='finish' if keep else 'cleanup', keep_warm=bool(keep), outcome='completed' if completed else 'failed',
                             response=result.get('message') or state.get('last_error') or 'Hermes stopped without a final answer.')
                await database(self.save, run_id, state)
        elif phase == 'waiting_credential':
            if not self.credentials or (await database(self.credentials.resolution, run_id,state['wait_credential']))['status']=='pending':
                return False  # Temporal waits on a durable wake, without a sandbox or polling.
            async with self.admission(run_id) as available:
                if not available:
                    return 'capacity'
                state.update(phase='provision',resume_credential=state['wait_credential'])
                await database(self.save, run_id,state)
            await database(self.running_status, run_id,'provisioning')
            await database(self.store.event, run_id,'credential','Credential request resolved. Resuming the saved session.')
        elif phase == 'waiting_children':
            if not self.coordinator or not self.coordinator.settled(run_id, state['wait_group']):
                return 'children'
            async with self.admission(run_id) as available:
                if not available:
                    return 'capacity'
                # Admission may await releasing another machine. A web user can
                # queue a worker follow-up during that wait; recheck atomically.
                if not self.coordinator.handoff(run_id, state['wait_group']):
                    return 'children'
                state.update(phase='provision', resume_group=state['wait_group'])
                await database(self.save, run_id, state)
            await database(self.running_status, run_id, 'provisioning')
            await database(self.store.event, run_id, 'agents', 'Workers finished. Restoring the coordinator to gather results.', {'group_id': state['wait_group']})
            await database(self.save, run_id, state)
        elif phase == 'cleanup':
            if self.coordinator and state.get('outcome') not in {'completed', 'steered'}:
                await self.coordinator.cancel_children(run_id)
            await self.cleanup(state, run_id)
            state.update(phase='finish', keep_warm=False, sandbox_id='')
            await database(self.store.update_run, run_id, sandbox_id='')
            await database(self.save, run_id, state)
        elif phase == 'finish':
            if state['outcome'] == 'steered':
                state['response'] = ''  # Also suppress notices checkpointed by an older worker.
            if stop_requested(run):
                state['outcome'] = 'cancelled'
                if state.get('keep_warm'):
                    state.update(phase='cleanup', keep_warm=False)
                    await database(self.save, run_id, state)
                    return True
            # Older cleanup could interrupt the source before this journal
            # finished. Its owner must retire the receipt with that outcome.
            retired = await database(self.store.rows, """SELECT a.content,m.status FROM messages a
                JOIN messages m ON m.id=a.response_to_id AND m.run_id=a.run_id
                WHERE a.run_id=? AND a.response_to_id=? AND a.status='saving'
                AND m.status IN ('completed','failed','cancelled','interrupted','steered','save_failed')""",
                (run_id, state['message_id']))
            if retired:
                state.update(outcome=retired[0]['status'], response='' if retired[0]['status'] == 'steered' else retired[0]['content'])
                if state['outcome'] not in {'completed', 'steered'} and state.get('keep_warm'):
                    state.update(phase='cleanup', keep_warm=False)
                    await database(self.save, run_id, state)
                    return True
            await database(self.store.finish_message, run_id, state['message_id'], state['response'], state['outcome'])
            if self.credentials and hasattr(self.credentials, 'reconcile_resolutions'):
                self.credentials.reconcile_resolutions(run_id)
            status = 'idle' if state['outcome'] in {'completed', 'steered'} else ('failed' if state['outcome'] == 'save_failed' else state['outcome'])
            if status != 'idle':
                await database(self.store.execute, "UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
            await database(self.store.update_run, run_id, status='queued' if status == 'idle' and (await database(self.store.has_queued_messages, run_id)) else status,
                                  token_hash='', summary=state['response'])
            await database(self.store.execute, "UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
            state['phase'] = 'warm' if state.get('keep_warm') else 'idle'
            if state['phase'] == 'warm':
                # Queued work has not gone idle, even if the worker restarts
                # before it can claim the next message.
                state['idle_until'] = (None if (await database(self.store.has_queued_messages, run_id))
                                       else time.time() + self.settings.sandbox_idle_seconds)
            await database(self.save, run_id, state)
            if state['phase'] == 'warm' and not (await database(self.store.has_queued_messages, run_id)):
                return {'idle_seconds': max(0.01, self.idle_deadline(state) - time.time())}
            return await database(self.store.has_queued_messages, run_id)
        return True

    def acknowledge_credential(self, run_id, state):
        receipt = state.pop('credential_delivery', None)
        if receipt and self.credentials:
            self.credentials.acknowledge(run_id, state['message_id'], receipt['request_id'], receipt['generation'])
            state.pop('resume_credential', None)
            state.pop('wait_credential', None)

    async def cleanup(self, state, run_id):
        name = state.get('sandbox_name')
        if not name and state.get('computer_only'):
            name = self.provision_name(run_id, state)
        if name and not state.get('sandbox_id'):
            # Recover a create whose acknowledgement was lost before Stop.
            try:
                sandbox = await self.provider(self.store.run(run_id)).find(name)
                state['sandbox_id'] = sandbox.object_id
                self.save(run_id, state)
            except modal.exception.NotFoundError:
                state.pop('sandbox_name', None)
                return
        await super().cleanup(state, run_id)
        state.pop('sandbox_name', None)
