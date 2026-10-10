"""Join durable agent turns to the existing session/computer lifecycle."""
import asyncio
from datetime import timedelta
import json
import time

from pydantic_ai.messages import ModelMessagesTypeAdapter
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .db import database
from .pydantic_workflow import PydanticTurnWorkflow, TurnScope, TurnResult
from .security import digest
from .model_selection import gateway_model


class PydanticExecution:
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
            identity = f'moyai-pydantic-{run_id}-{scope.message_id}-{scope.segment}'
            state.update(pydantic_workflow_id=identity, machine_started=state['turn_started'])
            await database(r.save, run_id, state)  # Same ID even after a lost start ACK.
            remaining = (r.settings.run_timeout_seconds - (time.time() - state['turn_started'])
                         if r.settings.run_timeout_seconds else None)
            if remaining is not None and remaining <= 0:
                await database(r.fail, run_id, state, 'This response reached its configured time limit.')
                return True
            try:
                await r.temporal.start_workflow(PydanticTurnWorkflow.run, scope, id=identity,
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
        await r.agent_runtime.computer_step(run_id, state)
        handle = r.temporal.get_workflow_handle(state['pydantic_workflow_id'], result_type=TurnResult)
        description = await handle.describe()
        if description.status == WorkflowExecutionStatus.RUNNING:
            return {'retry_seconds': 1}
        try:
            completed = await handle.result()
        except WorkflowFailureError:
            await database(r.fail, run_id, state,
                'The durable agent could not continue. Completed results are saved. '
                'Inspect saved action receipts before retrying any unconfirmed write.')
            return True
        scope = [run_id, state['message_id'], run['active_user_id'],
                 gateway_model(r.settings.resolve_model(fallback=run['active_model'] or run['model']))]
        checkpoint = json.dumps({'scope': scope, 'messages': ModelMessagesTypeAdapter.dump_json(completed.messages).decode(),
                                 'applied': completed.applied}).encode()
        pointer = await asyncio.to_thread(self.payloads.put, checkpoint)
        result = {**completed.result, 'message_id': state['message_id']}
        await database(r.message_queue.acknowledge, run_id, state['message_id'], completed.applied)
        await database(r.receive_result, run_id, result)
        state.update(phase='save', pydantic_checkpoint=pointer, result=result, exit_code=0)
        await database(r.save, run_id, state)
        return True

    async def cancel(self, state):
        if identity := state.get('pydantic_workflow_id'):
            try:
                await self.runner.temporal.get_workflow_handle(identity).cancel()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
