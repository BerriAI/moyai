"""Join durable agent turns to the existing session/computer lifecycle."""
import asyncio
from datetime import timedelta
import json
import time

from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .db import database
from .deepagents_diagnostics import workflow_failure
from .deepagents_workflow import DeepAgentsTurnWorkflow, TurnScope, TurnResult
from .security import digest
from .model_selection import gateway_model


class DeepAgentsExecution:
    def __init__(self, runner, payloads):
        self.runner, self.payloads = runner, payloads

    async def step(self, run_id, state):
        r = self.runner
        run = await database(r.store.run, run_id)
        if state['phase'] in {'provision', 'install', 'launch'}:
            if state.get('context_pending') and r.prepare_context:
                await r.prepare_context(run_id)
            state.pop('context_pending', None)
            await r.swarms.enforce(run_id)
            if not await database(r.running_status, run_id, 'running', digest(r.token(run_id, state['message_id']))):
                return True
            scope = TurnScope(run_id, state['message_id'], state['segment'], run['active_user_id'])
            identity = f'moyai-deepagents-{run_id}-{scope.message_id}-{scope.segment}'
            state.update(deepagents_workflow_id=identity, machine_started=state['turn_started'])
            await database(r.save, run_id, state)  # Same ID even after a lost start ACK.
            remaining = (r.settings.run_timeout_seconds - (time.time() - state['turn_started'])
                         if r.settings.run_timeout_seconds else None)
            if remaining is not None and remaining <= 0:
                await database(r.fail, run_id, state, 'This response reached its configured time limit.')
                return True
            try:
                await r.temporal.start_workflow(DeepAgentsTurnWorkflow.run, scope, id=identity,
                    task_queue=r.settings.temporal_task_queue + '-agents-v1',
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(seconds=remaining) if remaining else None)
            except WorkflowAlreadyStartedError:
                pass  # A completed execution is also a durable launch receipt.
            state['phase'] = 'monitor'
            await database(r.save, run_id, state)
            return True
        await r.swarms.enforce(run_id)
        if r.settings.run_timeout_seconds and time.time() - state['turn_started'] >= r.settings.run_timeout_seconds:
            await database(r.fail, run_id, state, 'This response reached its configured time limit.')
            return True
        await self.steer(run_id)
        if state.get('computer_requested'):
            try:
                await r.temporal.get_workflow_handle(state['deepagents_workflow_id']).signal(DeepAgentsTurnWorkflow.request_computer)
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
        handle = r.temporal.get_workflow_handle(state['deepagents_workflow_id'], result_type=TurnResult)
        description = await handle.describe()
        if description.status == WorkflowExecutionStatus.RUNNING:
            return {'retry_seconds': 1}
        try:
            completed = await handle.result()
        except WorkflowFailureError as exc:
            await database(r.store.event, run_id, 'error', 'Durable agent workflow failed.',
                {'phase': 'sdk_failure', **workflow_failure(exc)}, turn_id=state['message_id'])
            await database(r.fail, run_id, state,
                'The durable agent could not continue. Completed results are saved. '
                'Inspect saved action receipts before retrying any unconfirmed write.')
            return True
        scope = [run_id, state['message_id'], run['active_user_id'],
                 gateway_model(r.settings.resolve_model(fallback=run['active_model'] or run['model']))]
        checkpoint = json.dumps({'scope': scope, 'messages': completed.messages,
                                 'applied': completed.applied}).encode()
        pointer = await asyncio.to_thread(self.payloads.put, checkpoint)
        result = {**completed.result, 'message_id': state['message_id']}
        state.pop('computer_requested', None)
        await database(r.message_queue.acknowledge, run_id, state['message_id'], completed.applied)
        await database(r.receive_result, run_id, result)
        state.update(phase='save', deepagents_checkpoint=pointer, result=result, exit_code=0)
        await database(r.save, run_id, state)
        return True

    async def steer(self, run_id):
        r = self.runner
        state = await database(r.state, run_id)
        identity = state.get('deepagents_workflow_id')
        if identity and await database(r.message_queue.accept_steer, run_id, state['message_id']):
            try:
                await r.temporal.get_workflow_handle(identity).signal(DeepAgentsTurnWorkflow.steer)
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise

    async def cancel(self, state):
        if identity := state.get('deepagents_workflow_id'):
            try:
                await self.runner.temporal.get_workflow_handle(identity).cancel()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
