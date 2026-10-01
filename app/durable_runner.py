"""Idempotent session steps. SQLite owns data; Temporal owns scheduling/retries.

Supported topology is one Render instance/one worker on the same disk. Moving
workers to another service requires shared storage first, not a second SQLite.
"""
import asyncio
import hashlib
import hmac
import json
import time

import modal

from .runner import RunManager, SANDBOX_FILES, SAVE_WARNING, TERMINAL, safe_error_detail
from .security import digest


class LostExecution(Exception):
    pass


class DurableRunner(RunManager):
    def __init__(self, store, settings):
        super().__init__(store, settings)
        self.locks = {}
        self.admission_lock = asyncio.Lock()
        store.execute("""CREATE TABLE IF NOT EXISTS durable_sessions (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), state TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0)""")

    def state(self, run_id):
        rows = self.store.rows('SELECT state FROM durable_sessions WHERE run_id=?', (run_id,))
        return json.loads(rows[0]['state']) if rows else {}

    def save(self, run_id, state):
        self.store.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))

    def running_status(self, run_id, status, token_hash=None):
        # Never overwrite a stop that arrived during an awaited provider call.
        if self.store.run(run_id)['status'] == 'stopping':
            return False
        fields = {'status': status}
        if token_hash is not None:
            fields['token_hash'] = token_hash
        self.store.update_run(run_id, **fields)
        return True

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

    async def advance(self, run_id):
        async with self.locks.setdefault(run_id, asyncio.Lock()):
            row = self.store.run(run_id)
            if not row:
                return False
            state = self.state(run_id)
            if state.get('phase') in {'warm', 'warm_cleanup'}:
                reuse = await self.warm(run_id, state)
                if reuse is not True:
                    return reuse
                row = self.store.run(run_id)
                state = self.state(run_id)
                if state.get('phase') == 'warm':
                    # A queued follow-up claims the already occupied slot.
                    if not self.begin_turn(run_id, row, state):
                        return True
                    state = self.state(run_id)
            if not state or state.get('phase') == 'idle':
                if row['status'] in {'cancelled', 'interrupted', 'stopping'}:
                    if row['status'] == 'stopping':
                        self.store.update_run(run_id, status='cancelled')
                    return False
                if not self.store.has_queued_messages(run_id) and not self.store.rows(
                        "SELECT id FROM messages WHERE run_id=? AND status='running'", (run_id,)):
                    return False
                async with self.admission_lock:
                    if not await self.make_capacity(run_id):
                        return 'capacity'
                    if not self.begin_turn(run_id, self.store.run(run_id)):
                        return True
                    state = self.state(run_id)
                if state.get('phase') == 'idle' or not state:
                    return False
            # Cancellation is persistent, including across worker restarts.
            if self.store.run(run_id)['status'] == 'stopping' and state['phase'] not in {'cleanup', 'finish'}:
                self.fail(run_id, state, 'The response was stopped. The last saved workspace is preserved.', 'cancelled')
            try:
                return await self.step(run_id, state)
            except LostExecution:
                if ((state.get('reused_machine') and state['phase'] in {'prepare', 'install'})
                        or (state.get('startup_attempt') and state['phase'] == 'install'
                            and not state.get('execution_started'))):
                    # This new turn has not reached its launch boundary yet.
                    state.update(phase='provision', sandbox_id='', reused_machine=False)
                    self.save(run_id, state)
                    return True
                self.fail(run_id, state,
                          'The cloud process stopped before its work could be confirmed. The last checkpoint is preserved. '
                          'No unfinished actions were replayed; send a new message to inspect and continue.', 'interrupted')
                return True
            except ValueError:
                self.fail(run_id, state, 'This response cannot run with the current workspace configuration. '
                          'An administrator must check the selected model and runtime settings.')
                return True

    def begin_turn(self, run_id, row, warm=None):
        if row['status'] in {'stopping', 'cancelled', 'interrupted'}:
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
        self.save(run_id, state)
        return True

    def idle_deadline(self, state):
        # A follow-up never extends the machine's absolute renewal deadline.
        return min(state.get('idle_until') or float('inf'),
                   state['machine_started'] + self.settings.sandbox_rotation_seconds)

    async def release_warm(self, run_id, state, reason):
        # Persist before the provider call: a lost termination ACK is retryable
        # and never publishes the previous answer a second time.
        state.update(phase='warm_cleanup', idle_reason=reason)
        self.save(run_id, state)
        await self.cleanup(state)
        state.update(phase='idle', sandbox_id='')
        state.pop('idle_until', None)
        self.save(run_id, state)
        self.store.update_run(run_id, sandbox_id='', token_hash='')
        self.store.event(run_id, 'status', 'Idle sandbox released: ' + reason + '.')

    async def warm(self, run_id, state):
        row = self.store.run(run_id)
        stopping = row['status'] in {'stopping', 'cancelled', 'interrupted'}
        queued = self.store.has_queued_messages(run_id)
        expired = (time.time() >= state['machine_started'] + self.settings.sandbox_rotation_seconds
                   or (not queued and time.time() >= self.idle_deadline(state)))
        if (state['phase'] == 'warm_cleanup' or stopping or not self.settings.sandbox_idle_seconds
                or expired):
            await self.release_warm(run_id, state, state.get('idle_reason', 'idle timeout' if not stopping else 'stop requested'))
            row = self.store.run(run_id)
            stopping = row['status'] in {'stopping', 'cancelled', 'interrupted'}
            if row['status'] == 'stopping':
                self.store.update_run(run_id, status='cancelled')
            return False if stopping else self.store.has_queued_messages(run_id)
        if not queued:
            if state.get('idle_until') is None:
                state['idle_until'] = time.time() + self.settings.sandbox_idle_seconds
                self.save(run_id, state)
            return {'idle_seconds': max(0.01, self.idle_deadline(state) - time.time())}
        state['idle_until'] = None
        self.save(run_id, state)
        try:
            await self.sandbox(state)
        except LostExecution:
            await self.release_warm(run_id, state, 'machine no longer available')
            return True
        return True

    async def make_capacity(self, run_id):
        # Caller holds admission_lock until its own slot is persisted. Skip
        # busy per-session locks rather than deadlocking competing admissions.
        candidates = [(r['run_id'], json.loads(r['state'])) for r in self.store.rows('SELECT run_id,state FROM durable_sessions')]
        for other, state in sorted(candidates, key=lambda entry: entry[1].get('idle_until') or float('inf')):
            if self.has_capacity():
                return True
            lock = self.locks.setdefault(other, asyncio.Lock())
            if other == run_id or lock.locked() or state.get('phase') not in {'warm', 'warm_cleanup'}:
                continue
            async with lock:
                if self.store.has_queued_messages(other):
                    continue
                await self.release_warm(other, state, 'capacity needed by another session')
        return self.has_capacity()

    def has_capacity(self):
        active = sum(json.loads(r['state']).get('phase', 'idle') not in {'idle', 'waiting_children', 'waiting_credential'}
                     for r in self.store.rows('SELECT state FROM durable_sessions'))
        return active < self.settings.max_concurrent_runs

    async def snapshot_for_children(self, run):
        sandbox = await self.sandbox(self.state(run['id']))
        snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds, ttl=None)
        return snapshot.object_id

    def fail(self, run_id, state, explanation, status='failed'):
        preserved = self.preserve_answer(run_id)
        state.update(phase='cleanup', outcome='save_failed' if preserved else status,
                     response=self.store.run(run_id)['summary'] if preserved else explanation)
        self.store.update_run(run_id, token_hash='', error=explanation)
        self.save(run_id, state)
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))

    async def sandbox(self, state):
        try:
            sandbox = await modal.Sandbox.from_id.aio(state['sandbox_id'], client=await self.client())
        except modal.exception.NotFoundError:
            raise LostExecution() from None
        if await sandbox.poll.aio() is not None:
            raise LostExecution()
        return sandbox

    async def provision(self, run_id, state):
        client = await self.client()
        name = f"moyai-{run_id}-{state['message_id']}-{state['segment']}"
        try:
            sandbox = await modal.Sandbox.from_name.aio(self.settings.modal_app_name, name, client=client)
        except modal.exception.NotFoundError:
            app = await modal.App.lookup.aio(self.settings.modal_app_name, create_if_missing=True, client=client)
            image = modal.Image.from_id(state['snapshot_id'], client=client) if state['snapshot_id'] else self.image()
            try:
                sandbox = await modal.Sandbox.create.aio(
                    app=app, client=client, name=name, image=image,
                    secrets=[modal.Secret.from_dict({'WORKSPACE_RUN_TOKEN': self.token(run_id, state['message_id'])})],
                    env={'PYTHONUNBUFFERED': '1', 'PYTHONPATH': '/opt/hermes', 'HERMES_HOME': '/tmp/hermes-home',
                         'HERMES_RUNTIME_DIR': '/opt/hermes-tools', 'HERMES_PYTHON': '/opt/hermes-env/bin/python',
                         'GIT_TERMINAL_PROMPT': '0'},
                    timeout=86400, cpu=2, memory=4096,
                    experimental_options={'vm_runtime': True} if self.settings.modal_vm_runtime else {},
                )
            except modal.exception.AlreadyExistsError:
                sandbox = await modal.Sandbox.from_name.aio(self.settings.modal_app_name, name, client=client)
        state.update(sandbox_id=sandbox.object_id, machine_started=time.time(), phase='install')
        self.save(run_id, state)
        self.store.update_run(run_id, sandbox_id=sandbox.object_id)
        return sandbox

    def directory(self, state):
        suffix = f"-startup-{state['startup_attempt']}" if state.get('startup_attempt') else ''
        return f"/session/executions/{state['message_id']}-{state['segment']}{suffix}"

    def startup_retry_allowed(self, run, state):
        marker = state.get('result', {}).get('startup_retry')
        return (isinstance(marker, dict) and marker.get('version') == 1
                and marker.get('stage') in {'workspace_tools', 'attachments'}
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

    async def command(self, sandbox, *args, token=None):
        # Pass capabilities only in the exec environment, never in a persisted
        # task spec, command argument or Temporal payload. Reused machines have
        # an older creation-time environment, so every launch overrides it.
        process = await sandbox.exec.aio('/usr/local/bin/python', '/opt/workspace-runner/durable_process.py', *args,
                                         timeout=30, env={'WORKSPACE_RUN_TOKEN': token} if token else {})
        output, _ = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
        if await process.wait.aio() != 0:
            raise RuntimeError('Sandbox supervisor command failed')
        return output

    async def step(self, run_id, state):
        phase = state['phase']
        run = self.store.run(run_id)
        if phase in {'waiting_children', 'waiting_credential'}:
            control = self.message_queue.live_control(run_id, state['message_id'], [], checkpointed=True)
            if control.get('handoff'):
                # A different requester/model still needs a capability boundary.
                # The checkpoint is already durable; no synthetic chat answer.
                state.update(phase='finish', outcome='steered', response='', keep_warm=False)
                self.save(run_id, state)
                return True
            if control.get('input'):
                async with self.admission_lock:
                    if not await self.make_capacity(run_id):
                        return 'capacity'
                    if self.store.run(run_id)['status'] == 'stopping':
                        return True
                    # Resume the saved conversation under the same turn and scope.
                    # Leave the input pending until the restored agent acknowledges
                    # native delivery; a worker restart cannot lose the correction.
                    state.update(phase='provision', resume_group=state.get('wait_group'),
                                 resume_credential=state.get('wait_credential'))
                    self.save(run_id, state)
                self.running_status(run_id, 'provisioning')
                return True
        if phase == 'prepare':
            if self.prepare_context:
                await self.prepare_context(run_id)
            if self.store.run(run_id)['status'] == 'stopping':
                return True
            if run['mode'] == 'demo':
                await self.demo(run)
                if self.store.run(run_id)['status'] == 'stopping':
                    self.fail(run_id, state, 'The response was stopped.', 'cancelled')
                    return True
                row = self.store.run(run_id)
                state.update(phase='finish', outcome='steered' if row['status']=='steered' else 'completed', response=row['summary'])
            else:
                state['phase'] = 'install' if state.get('sandbox_id') else 'provision'
                self.running_status(run_id, 'running' if state.get('sandbox_id') else 'provisioning')
            self.save(run_id, state)
        elif phase == 'provision':
            await self.provision(run_id, state)
        elif phase == 'install':
            sandbox = await self.sandbox(state)
            # Refresh protocol adapters on snapshots from older releases.
            for path in SANDBOX_FILES.glob('*.py'):
                await sandbox.filesystem.write_text.aio(path.read_text(), '/opt/workspace-runner/' + path.name)
            message = self.store.rows('SELECT content FROM messages WHERE id=?', (state['message_id'],))[0]
            spec = self.spec({**run, 'prompt': message['content'], 'message_id': state['message_id'],
                              'continuation': state['segment'] > 0})
            spec['rotation_seconds'] = self.settings.temporal_checkpoint_seconds
            spec['chat_enabled'] = True
            if state.get('resume_group') and self.coordinator:
                spec['agent_results'] = self.coordinator.results(run_id, state['resume_group'])
            if state.get('resume_credential') and self.credentials:
                spec['credential_resolution'] = self.credentials.resolution(run_id,state['resume_credential'])
            if self.settings.run_timeout_seconds:
                remaining = self.settings.run_timeout_seconds - (time.time() - state['turn_started'])
                if remaining <= 0:
                    self.fail(run_id, state, 'This response reached its configured time limit.')
                    return True
                spec['timeout'] = remaining
            # Each piece gets its own immutable input and launch marker.
            spec_path = '/tmp/moyai-' + self.directory(state).rsplit('/', 1)[-1] + '.json'
            await sandbox.filesystem.write_text.aio(json.dumps(spec), spec_path)
            state.update(phase='launch', startup_model_calls=self.store.run(run_id)['turn_model_calls'],
                         execution_started=False)
            self.save(run_id, state)
        elif phase == 'launch':
            sandbox = await self.sandbox(state)
            if not self.running_status(run_id, 'running', digest(self.token(run_id, state['message_id']))):
                return True
            spec_path = '/tmp/moyai-' + self.directory(state).rsplit('/', 1)[-1] + '.json'
            await self.command(sandbox, 'start', self.directory(state), spec_path, token=self.token(run_id, state['message_id']))
            state['phase'] = 'monitor'
            self.save(run_id, state)
        elif phase == 'monitor':
            sandbox = await self.sandbox(state)
            # One Activity may poll multiple times, keeping workflow history
            # small. Heartbeats are delivered independently by the worker.
            for _ in range(10):
                if self.store.run(run_id)['status'] == 'stopping':
                    return True
                output = await self.command(sandbox, 'read', self.directory(state), str(state['cursor']))
                for value in (self.token(run_id, state['message_id']), self.settings.litellm_api_key, self.settings.modal_token_secret):
                    if value:
                        output = output.replace(value, '[redacted]')
                report = json.loads(output)
                for event in report['events']:
                    if event.get('kind') in {'tool', 'status', 'error', 'message'}:
                        self.store.event(run_id, event['kind'], str(event.get('message', '')), event.get('data', {}))
                    event_phase = event.get('data', {}).get('phase')
                    if event_phase == 'execution_started':
                        state['execution_started'] = True
                        state.pop('startup_deadline', None)
                        self.running_status(run_id, 'running')
                    elif event_phase == 'reconnecting' and not state.get('execution_started'):
                        self.running_status(run_id, 'reconnecting')
                    if event.get('kind') == 'error':
                        state['last_error'] = str(event.get('message', ''))
                state['cursor'] = report['cursor']
                if report.get('final'):
                    self.message_queue.acknowledge(run_id, state['message_id'], report['final'].get('steering_applied', []))
                    state['result'] = {**report['final'], 'message_id': state['message_id']}
                    if not state['result'].get('startup_retry'):
                        self.store.update_run(run_id, summary=str(report['final'].get('message', '')),
                                              pending_result=json.dumps(state['result']))
                self.save(run_id, state)
                if report['state'] == 'uncertain':
                    raise LostExecution()
                if report['state'] == 'new':
                    # A launch RPC may have succeeded without its process
                    # starting yet. The supervisor safely arbitrates retries.
                    state['phase'] = 'launch'
                    self.save(run_id, state)
                    return True
                if report['state'] == 'done' and len(report['events']) < 30:
                    state['exit_code'] = report['exit_code']
                    if self.startup_retry_allowed(self.store.run(run_id), state):
                        self.wait_for_startup(run_id, state)
                        return True
                    if state.get('result', {}).get('startup_retry'):
                        self.fail(run_id, state, 'The agent stopped, but a safe startup retry could not be confirmed. '
                                  'No potentially completed work was replayed.')
                        return True
                    state['phase'] = 'save'
                    self.save(run_id, state)
                    return True
                await asyncio.sleep(2)
        elif phase == 'startup_wait':
            if time.time() >= state['startup_deadline']:
                self.wait_for_startup(run_id, state)
                return True
            remaining = state['retry_at'] - time.time()
            if remaining > 0:
                return {'retry_seconds': remaining}
            state.update(phase='install', cursor=0)
            for key in ('result', 'exit_code', 'last_error', 'save_attempts'):
                state.pop(key, None)
            self.save(run_id, state)
        elif phase == 'save':
            sandbox = await self.sandbox(state)
            result = state.get('result', {})
            # Answer persisted above before archive/snapshot work.
            await self.save_artifact(sandbox, run_id)
            if not self.running_status(run_id, 'saving', ''):
                return True
            try:
                snapshot = await sandbox.snapshot_filesystem.aio(timeout=self.settings.snapshot_timeout_seconds, ttl=None)
            except Exception as exc:
                state['save_attempts'] = state.get('save_attempts', 0) + 1
                self.save(run_id, state)
                self.store.event(run_id, 'error', 'Workspace checkpoint could not be confirmed.',
                                 {'stage': 'snapshot_filesystem', 'attempt': state['save_attempts'],
                                  'reason': safe_error_detail(exc, [self.settings.modal_token_secret])})
                if state['save_attempts'] >= 3:
                    self.fail(run_id, state, SAVE_WARNING if result else 'Workspace saving failed. No unfinished work was replayed.')
                    return True
                raise
            result.update(checkpoint_saved=True, exit_code=state['exit_code'])
            state.update(snapshot_id=snapshot.object_id, phase='checkpointed', result=result)
            # Commit checkpoint and protocol phase together so a crash cannot
            # roll a turn forward without the matching conversation snapshot.
            with self.store.connect() as conn:
                conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
                conn.execute("UPDATE runs SET snapshot_id=?,checkpoint_error='',pending_result=? WHERE id=?",
                             (snapshot.object_id, json.dumps(result), run_id))
        elif phase == 'checkpointed':
            result = state['result']
            continuing = result.get('continuation') and state['exit_code'] == 0
            steered = (state['exit_code'] == 0 and
                       self.message_queue.accepted(run_id, result.get('steer_message_id')))
            if steered:
                keep = (self.settings.sandbox_idle_seconds and not run['parent_run_id']
                        and time.time() < state['machine_started'] + self.settings.sandbox_rotation_seconds)
                state.update(phase='finish' if keep else 'cleanup', keep_warm=bool(keep), outcome='steered', response='')
                self.save(run_id, state)
            elif continuing and result.get('wait_credential') and self.credentials:
                self.credentials.resolution(run_id,result['wait_credential'])
                await self.cleanup(state)
                state.update(phase='waiting_credential',wait_credential=result['wait_credential'],sandbox_id='',
                             segment=state['segment']+1,cursor=0)
                state.pop('result',None)
                state.pop('save_attempts',None)
                self.running_status(run_id,'waiting_credential','')
                self.store.update_run(run_id,pending_result='',summary='',sandbox_id='')
                self.store.event(run_id,'credential','Workspace saved. Waiting for a provider key; sandbox released.',
                                 {'request_id':state['wait_credential']})
                self.save(run_id,state)
            elif continuing and result.get('wait_group') and self.coordinator:
                # Validate the control message against persisted ownership.
                self.coordinator.group(run_id, result['wait_group'])
                await self.cleanup(state)
                state.update(phase='waiting_children', wait_group=result['wait_group'], sandbox_id='',
                             segment=state['segment'] + 1, cursor=0)
                state.pop('result', None)
                state.pop('save_attempts', None)
                self.running_status(run_id, 'waiting_children', '')
                self.store.update_run(run_id, pending_result='', summary='')
                self.store.event(run_id, 'agents', 'Workspace saved. Waiting for parallel agents; coordinator sandbox released.',
                                 {'group_id': state['wait_group']})
                self.save(run_id, state)
            elif continuing:
                if time.time() - state['machine_started'] >= self.settings.sandbox_rotation_seconds:
                    await self.cleanup(state)
                    state.update(sandbox_id='', phase='provision')
                else:
                    state['phase'] = 'install'
                state.update(segment=state['segment'] + 1, cursor=0)
                state.pop('result', None)
                state.pop('save_attempts', None)
                self.running_status(run_id, 'running')
                self.store.update_run(run_id, pending_result='', summary='')
                self.store.event(run_id, 'status', 'Workspace checkpoint saved. Continuing the same request.')
                self.save(run_id, state)
            else:
                completed = result.get('completed') and state['exit_code'] == 0
                keep = (completed and self.settings.sandbox_idle_seconds and run['chat_enabled'] and not run['parent_run_id']
                        and time.time() < state['machine_started'] + self.settings.sandbox_rotation_seconds)
                state.update(phase='finish' if keep else 'cleanup', keep_warm=bool(keep), outcome='completed' if completed else 'failed',
                             response=result.get('message') or state.get('last_error') or 'Hermes stopped without a final answer.')
                self.save(run_id, state)
        elif phase == 'waiting_credential':
            if not self.credentials or self.credentials.resolution(run_id,state['wait_credential'])['status']=='pending':
                return False  # Temporal waits on a durable wake, without a sandbox or polling.
            async with self.admission_lock:
                if not await self.make_capacity(run_id):
                    return 'capacity'
                state.update(phase='provision',resume_credential=state['wait_credential'])
                self.save(run_id,state)
            self.running_status(run_id,'provisioning')
            self.store.event(run_id,'credential','Credential request resolved. Resuming the saved session.')
        elif phase == 'waiting_children':
            if not self.coordinator or not self.coordinator.settled(run_id, state['wait_group']):
                return 'children'
            async with self.admission_lock:
                if not await self.make_capacity(run_id):
                    return 'capacity'
                # Admission may await releasing another machine. A web user can
                # queue a worker follow-up during that wait; recheck atomically.
                if not self.coordinator.handoff(run_id, state['wait_group']):
                    return 'children'
                state.update(phase='provision', resume_group=state['wait_group'])
                self.save(run_id, state)
            self.running_status(run_id, 'provisioning')
            self.store.event(run_id, 'agents', 'Workers finished. Restoring the coordinator to gather results.', {'group_id': state['wait_group']})
            self.save(run_id, state)
        elif phase == 'cleanup':
            if self.coordinator and state.get('outcome') not in {'completed', 'steered'}:
                await self.coordinator.cancel_children(run_id)
            await self.cleanup(state)
            state.update(phase='finish', keep_warm=False, sandbox_id='')
            self.store.update_run(run_id, sandbox_id='')
            self.save(run_id, state)
        elif phase == 'finish':
            if state['outcome'] == 'steered':
                state['response'] = ''  # Also suppress notices checkpointed by an older worker.
            if run['status'] == 'stopping':
                state['outcome'] = 'cancelled'
                if state.get('keep_warm'):
                    state.update(phase='cleanup', keep_warm=False)
                    self.save(run_id, state)
                    return True
            self.store.finish_message(run_id, state['message_id'], state['response'], state['outcome'])
            status = 'idle' if state['outcome'] in {'completed', 'steered'} else ('failed' if state['outcome'] == 'save_failed' else state['outcome'])
            if status != 'idle':
                self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
            self.store.update_run(run_id, status='queued' if status == 'idle' and self.store.has_queued_messages(run_id) else status,
                                  token_hash='', summary=state['response'])
            self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
            state['phase'] = 'warm' if state.get('keep_warm') else 'idle'
            if state['phase'] == 'warm':
                # Queued work has not gone idle, even if the worker restarts
                # before it can claim the next message.
                state['idle_until'] = (None if self.store.has_queued_messages(run_id)
                                       else time.time() + self.settings.sandbox_idle_seconds)
            self.save(run_id, state)
            if state['phase'] == 'warm' and not self.store.has_queued_messages(run_id):
                return {'idle_seconds': max(0.01, self.idle_deadline(state) - time.time())}
            return self.store.has_queued_messages(run_id)
        return True

    async def cleanup(self, state):
        if not state.get('sandbox_id'):
            return
        try:
            sandbox = await modal.Sandbox.from_id.aio(state['sandbox_id'], client=await self.client())
            if await sandbox.poll.aio() is None:
                await self.terminate(sandbox)
        except modal.exception.NotFoundError:
            pass  # A confirmed missing machine cannot still execute a tool.
