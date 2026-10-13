"""Computer acquisition hooks for the existing durable session controller.

All methods run under DurableRunner's session lease. Runtime and computer have
separate identities, but goals, inference, steering and answer settlement retain
the original turn and its capability.
"""
import hashlib
import json
import time

import modal
from fastapi import HTTPException

from sandbox.broker_failure import safe_error
from sandbox.project_environment import REPAIR_NOTICE
from .computer_operations import ComputerOperations
from .db import database
from .environments import EnvironmentPending
from .runner import refresh_sandbox_files, stop_requested
from .sandboxes.modal import ModalProvider
from .workspace_image import agent_runtime_image


def receipt(message, **details):
    return {'content': [{'type': 'text', 'text': json.dumps({'message': message, **details})}], 'isError': True}


class IndependentRuntime:
    def __init__(self, runner):
        self.runner = runner
        self.operations = ComputerOperations(runner.store, runner.submit_in)

    def supports(self, run):
        if run['harness'] == 'deepagents-temporal':
            # The graph runs on workers for every computer provider. Only
            # Codex uses the Modal control guest below; ensure_computer routes
            # Deep Agents tools through DurableRunner's selected provider.
            return run['mode'] == 'modal'
        return (self.runner.settings.independent_agent_runtime and run['mode'] == 'modal'
                and run['harness'] == 'codex'
                and (run.get('sandbox_provider') or self.runner.settings.sandbox_provider) == 'modal')

    def idle_seconds(self):
        settings = self.runner.settings
        return min(300, settings.sandbox_idle_seconds) if (
            settings.independent_agent_runtime and settings.codex_runtime_reuse) else 0

    def scope(self, run):
        r = self.runner
        return [run['id'], run['active_user_id'],
                r.settings.resolve_model(fallback=run['active_model'] or run['model']), r.prepared.build]

    def reusable(self, run, state):
        if not self.supports(run):
            return False
        try:
            scope = self.scope(run)
        except ValueError:
            # A removed model ends reuse, not the successfully completed turn.
            # Fresh provisioning still validates through scope() directly.
            return False
        return state.get('runtime_scope') == scope

    def provider(self):
        backend = ModalProvider(self.runner.settings, self.runner.modal_clients)
        backend.image = agent_runtime_image
        return backend

    async def provision(self, run_id, state):
        r = self.runner
        name = f"moyai-runtime-{run_id}-{state['message_id']}-{state['segment']}"
        state.update(sandbox_name=name, runtime_scope=self.scope(await database(r.store.run, run_id)))
        await database(r.save, run_id, state)
        backend = self.provider()
        try:
            runtime = await backend.find(name)
        except modal.exception.NotFoundError:
            try:
                runtime = await backend.create(name=name, token=r.token(run_id, state['message_id']))
            except modal.exception.AlreadyExistsError:
                runtime = await backend.find(name)
        # runs.sandbox_id is exclusively the user's computer, never this guest.
        state.update(sandbox_id=runtime.object_id, machine_started=time.time(), phase='install')
        await database(r.save, run_id, state)
        return True

    async def ensure_computer(self, run_id, state):
        r = self.runner
        computer = state.setdefault('computer', {})
        if not computer:
            async with r.admission(run_id) as available:
                if not available:
                    return False
                run = await database(r.store.run, run_id)
                if stop_requested(run):
                    return False
                computer.update(phase='provision', computer_only=True,
                    message_id=state['message_id'], segment=f"{state['segment']}-computer-{state.get('computer_generation', 0)}",
                    snapshot_id=run['snapshot_id'], sandbox_id='')
                await database(r.save, run_id, state)

        def save(_run_id, value):
            state['computer'] = value
            r.save(run_id, state)

        def provisioned(_run_id, value):
            state['computer'] = value
            with r.store.connect(write_scope=run_id) as conn:
                conn.begin_write()
                conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
                conn.execute('UPDATE runs SET sandbox_id=? WHERE id=?', (value['sandbox_id'], run_id))

        if computer['phase'] in {'provision', 'waiting_environment'}:
            try:
                await r.provision(run_id, computer, save=save, provisioned=provisioned)
            except EnvironmentPending as pending:
                computer.update(phase='waiting_environment', environment_build=pending.build_id)
                await database(r.save, run_id, state)
            return False
        sandbox = await r.sandbox(computer)
        if computer['phase'] == 'install':
            await refresh_sandbox_files(sandbox)
            if getattr(r, 'computer', None):
                await r.computer.restore(sandbox, run_id, required=False)
            computer['phase'] = 'prepare'
            await database(r.save, run_id, state)
        if computer['phase'] == 'prepare':
            outcome = await self.job(run_id, state, sandbox, 'prepare', {'prepare_computer': True})
            if outcome is None:
                return False
            result = outcome['result']
            if outcome['status'] != 'completed' or result.get('error') or result.get('isError') or not result.get('workspace'):
                run = await database(r.store.run, run_id)
                if (run['harness'] == 'deepagents-temporal' and computer.get('preparation_input_id') !=
                        await database(self.preparation_input, run_id, state['message_id'])):
                    # The old job has ended. Only now can the corrected request
                    # start a fresh preparation in this same workspace.
                    computer.pop('preparation_input_id', None)
                    await database(r.save, run_id, state)
                    return False
                raise HTTPException(503, result.get('error') or 'Computer preparation could not be confirmed.')
            computer.update(phase='ready', workspace=result['workspace'])
            if result.get('project_health'):
                computer['project_health'] = result['project_health']
            await database(r.save, run_id, state)
            if computer.get('project_health', {}).get('status') == 'needs_repair':
                try:
                    await database(r.store.event, run_id, 'status', REPAIR_NOTICE,
                        {'phase': 'environment', 'project_health': computer['project_health']}, turn_id=state['message_id'])
                except Exception:
                    pass
        return computer['phase'] == 'ready'

    def preparation_input(self, run_id, turn_id):
        inputs = self.runner.store.rows("""SELECT id FROM messages
            WHERE run_id=? AND steering_parent_id=? AND status='injected'
            ORDER BY started_at DESC,id DESC LIMIT 1""", (run_id, turn_id))
        return inputs[0]['id'] if inputs else turn_id

    async def job(self, run_id, state, sandbox, identity, fields):
        r = self.runner
        run = await database(r.store.run, run_id)
        spec = await database(r.spec, {**run, 'message_id': state['message_id']})
        if run['harness'] == 'deepagents-temporal' and identity != 'finish':
            if identity == 'prepare':
                computer = state.setdefault('computer', {})
                if not computer.get('preparation_input_id'):
                    computer['preparation_input_id'] = await database(self.preparation_input, run_id, state['message_id'])
                    await database(r.save, run_id, state)
                input_id = computer['preparation_input_id']
                # A cancelled preparation may resume under the new input, but
                # a launched job must keep its original immutable specification.
                identity = f'prepare-{input_id}'
                parent = f"{run_id}:{state['message_id']}:{state['segment']}:{state.get('computer_generation', 0)}:{identity}"
                request_id = hashlib.sha256(parent.encode()).hexdigest()[:32]
            else:
                input_id = await database(self.operations.input_id, run_id, identity)
                request_id = identity
            spec['broker_tool_scope'] = {'input_id': input_id, 'request_id': request_id}
        spec.update(independent_runtime=False, computer_operation=True,
                    workspace=state.get('computer', {}).get('workspace', '/workspace'), **fields)
        if state.get('computer', {}).get('project_health'):
            spec['project_health'] = state['computer']['project_health']
        directory = (f"/session/computer-operations/{state['message_id']}-{state['segment']}-"
                     f"{state.get('computer_generation', 0)}-{identity}")
        spec_path = directory.replace('/session/computer-operations/', '/tmp/computer-')+'.json'
        await sandbox.filesystem.write_text.aio(json.dumps(spec), spec_path)
        await r.command(sandbox, 'start', directory, spec_path, token=r.token(run_id, state['message_id']))
        report = json.loads(await r.command(sandbox, 'read', directory, '0'))
        diagnostic = safe_error(report.get('diagnostic'))
        if diagnostic and report['state'] in {'done', 'uncertain'}:
            # Bind to the host-owned operation identity rather than trusting a
            # sandbox-supplied correlation. Save before terminating the guest.
            diagnostic = safe_error({**diagnostic, 'request_id': (spec.get('broker_tool_scope') or {}).get('request_id')})
            try:
                await database(r.store.event, run_id, 'error', 'Computer operation failed.',
                    {'phase': 'supervisor_failure', **diagnostic}, turn_id=state['message_id'])
            except Exception:
                pass  # A telemetry outage cannot change a confirmed receipt.
        details = {'diagnostic': diagnostic, 'error_type': diagnostic.get('exception_type')} if diagnostic else {}
        if report['state'] == 'uncertain':
            return {'status': 'unknown', 'result': {
                **receipt('The computer supervisor lost this operation. Inspect its effects before retrying.'), **details}}
        if report['state'] != 'done':
            return None
        result = report.get('final', {}).get('tool_result')
        if result is not None:
            return {'status': 'completed', 'result': result}
        return {'status': 'unknown', 'result': {**receipt(
            'The computer operation ended without a verified result. Inspect its effects before retrying.'), **details}}

    async def computer_step(self, run_id, state):
        from .durable_runner import LostExecution
        try:
            return await self._computer_step(run_id, state)
        except LostExecution:
            r = self.runner
            operation = await database(self.operations.pending, run_id)
            if operation:
                await database(self.operations.settle, operation,
                    'unknown' if operation['status'] == 'dispatched' else 'rejected',
                    receipt('The computer stopped. Completed receipts remain saved; inspect uncertain effects before retrying.'))
            state['computer'] = {}
            state['computer_generation'] = state.get('computer_generation', 0)+1
            state.pop('computer_requested', None)
            await database(r.store.update_run, run_id, sandbox_id='',
                checkpoint_error='The computer stopped before a new filesystem checkpoint. Inspect files and saved action receipts before retrying changes.')
            await database(r.save, run_id, state)

    async def _computer_step(self, run_id, state):
        r = self.runner
        operation = await database(self.operations.pending, run_id)
        if not operation and not state.get('computer_requested'):
            return
        run = await database(r.store.run, run_id)
        if operation and (operation['turn_id'], operation['actor_id'], operation['capability']) != (
                run['active_message_id'], run['active_user_id'], run['token_hash']):
            await database(self.operations.settle, operation,
                'unknown' if operation['status'] == 'dispatched' else 'cancelled',
                receipt('The execution owner changed. Inspect already-dispatched effects before retrying.'))
            return
        try:
            if not await self.ensure_computer(run_id, state):
                return
        except HTTPException as exc:
            if operation:
                await database(self.operations.settle, operation, 'rejected', receipt(str(exc.detail)))
            state.pop('computer_requested', None)
            await database(r.save, run_id, state)
            return
        state.pop('computer_requested', None)
        if not operation:
            await database(r.save, run_id, state)
            return
        computer = state['computer']
        if operation['status'] == 'dispatched' and operation['machine_id'] != computer['sandbox_id']:
            await database(self.operations.settle, operation, 'unknown', receipt('The original computer is unavailable. No action was replayed.'))
            return
        if not await database(self.operations.dispatch, run, operation, computer['sandbox_id']):
            return
        sandbox = await r.sandbox(computer)
        result = await self.job(run_id, state, sandbox, operation['request_id'],
                                {'name': operation['name'], 'arguments': json.loads(operation['arguments'])})
        if result is not None:
            if (operation['name'] in {'browser_screenshot', 'browser_record_stop'}
                    and result['status'] == 'completed' and not result['result'].get('isError')):
                try:
                    await r.computer.sync(sandbox, run_id)
                except Exception as exc:
                    # Storage owns upload retries. Preserve the confirmed native
                    # action if copying fails, rather than failing or blocking the turn.
                    persistence = {'status': 'unconfirmed', 'error_type': type(exc).__name__,
                                   'status_code': exc.status_code if isinstance(exc, HTTPException) else None}
                    notice = ('Capture completed, but saving it to the app could not be confirmed. '
                              'The original sandbox path is preserved in this receipt. Do not repeat the capture. '
                              'Normal Computer polling or workspace saving can retry the copy; use media_list to confirm.')
                    result['result'] = {**result['result'], 'content': [*result['result']['content'],
                        {'type': 'text', 'text': json.dumps({'capture_persistence': persistence, 'notice': notice})}]}
                    try:
                        await database(r.store.event, run_id, 'error', notice,
                                       {'stage': 'capture_sync', 'tool': operation['name'],
                                        'request_id': operation['request_id'], **persistence},
                                       turn_id=operation['turn_id'])
                    except Exception:
                        pass  # The authoritative receipt already carries this warning.
            await database(self.operations.settle, operation, result['status'], result['result'])
            health = result['result'].get('project_health')
            if result['status'] == 'completed' and health and health != computer.get('project_health'):
                computer['project_health'] = health
                try:
                    await database(r.save, run_id, state)
                    if health['status'] == 'needs_repair':
                        await database(r.store.event, run_id, 'status', REPAIR_NOTICE,
                            {'phase': 'environment', 'project_health': health}, turn_id=state['message_id'])
                except Exception:
                    pass

    async def checkpoint(self, run_id, state):
        r = self.runner
        computer = state.get('computer', {})
        if computer.get('sandbox_id'):
            sandbox = await r.sandbox(computer)
            finalized = await self.job(run_id, state, sandbox, 'finish', {'finish_computer': True,
                'response': state.get('result', {}).get('message', '')})
            if finalized is None:
                return True
            if finalized['status'] != 'completed' or not finalized['result'].get('saved'):
                raise HTTPException(503, 'The computer files could not be saved. The answer and tool receipts remain durable.')
            await r.save_artifact(sandbox, run_id)
            snapshot = await sandbox.snapshot_filesystem.aio(timeout=r.settings.snapshot_timeout_seconds, ttl=None)
            state['snapshot_id'] = snapshot.object_id
            await database(r.store.update_run, run_id, snapshot_id=snapshot.object_id, checkpoint_error='')
        # Codex's cold replay comes from the public journal, independently of
        # the ephemeral control filesystem. No-computer answers need no snapshot.
        result = {**state.get('result', {}), 'checkpoint_saved': True, 'exit_code': state['exit_code']}
        state.update(phase='checkpointed', result=result)
        with r.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            conn.execute('UPDATE durable_sessions SET state=? WHERE run_id=?', (json.dumps(state), run_id))
            conn.execute('UPDATE runs SET pending_result=? WHERE id=?', (json.dumps(result), run_id))
        return True

    async def cleanup_computer(self, run_id, state):
        r = self.runner
        computer = state.get('computer', {})
        if computer:
            # This enters the existing named-creation reconciliation and normal
            # computer cleanup, with no independent-runtime flag on the child.
            await r.cleanup(computer, run_id, save=lambda *_: r.save(run_id, state))
            state['computer'] = {}
        state.pop('computer_requested', None)
        await database(r.store.update_run, run_id, sandbox_id='')
        await database(r.save, run_id, state)

    async def cleanup(self, run_id, state):
        r = self.runner
        await self.cleanup_computer(run_id, state)
        identity, name = state.get('sandbox_id'), state.get('sandbox_name')
        if identity or name:
            try:
                sandbox = await self.provider().get(identity) if identity else await self.provider().find(name)
                await sandbox.terminate.aio()
                await sandbox.wait.aio(raise_on_termination=False)
            except modal.exception.NotFoundError:
                pass
        await database(self.operations.abandon, run_id)
        state.pop('sandbox_name', None)
        await database(r.save, run_id, state)
