"""Durable hosting for the native SDK graph, separate from the legacy adapter."""
import asyncio
from contextlib import aclosing
from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from temporalio.deepagents import TemporalModel
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict
    from .native_agent import Session, SessionModel, build_agent
    from .deepagents_workflow import TurnInput, TurnScope, TurnResult


class DurableSessionModel(TemporalModel):
    """Public Temporal model contract with Moyai's scoped model activity."""
    session: Session
    bind_tools = SessionModel.bind_tools
    _agenerate = SessionModel._agenerate
    # Streaming is performed inside the activity, where capability fencing and
    # the public event sink live. Never open a provider stream in the workflow.
    _astream = BaseChatModel._astream


class Services:
    def __init__(self, prepared):
        self.scope = prepared.scope
        self.queue = workflow.info().task_queue

    async def invoke(self, name, *args, side_effect=False, control=False):
        return await workflow.execute_activity(name, args=args,
            task_queue=self.queue + ('-control' if control else '-native-tools' if side_effect else '-native-model'),
            start_to_close_timeout=timedelta(minutes=1 if control else 720),
            heartbeat_timeout=None if control else timedelta(seconds=10),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(maximum_attempts=1 if side_effect else 3))

    async def boundary(self, applied, answer, receipt_only=False):
        return await self.invoke('deepagents_boundary_v1', self.scope, applied, 0.0,
                                 answer, receipt_only, control=True)

    async def model(self, messages, settings, tools, identity, input_id):
        raw = await self.invoke('native_agent_model_v1', self.scope, messages_to_dict(messages),
            {**settings, 'moyai_public_id': identity, 'moyai_input_id': input_id}, tools)
        return messages_from_dict([raw])[0]

    async def call_tool(self, identity, name, arguments, input_id):
        return await self.invoke('native_agent_tool_v1', replace(self.scope, input_id=input_id),
                                 identity, name, arguments, side_effect=True)

    async def publish(self, identity, text, input_id):
        await self.invoke('deepagents_publish_v1', self.scope, identity, text, input_id, control=True)

    async def activity(self, identity, name, phase, input_id):
        await self.invoke('native_agent_activity_v1', self.scope, identity, name, phase, input_id, control=True)


@workflow.defn(name='NativeDeepAgentsTurnV1')
class NativeAgentWorkflow:
    def __init__(self):
        self.revision = 0
        self.computer_requested = False

    @workflow.signal
    def steer(self):
        self.revision += 1

    @workflow.signal
    def request_computer(self):
        self.computer_requested = True

    @workflow.run
    async def run(self, scope: TurnScope) -> TurnResult:
        # Capture admission separately so a retried prepare uses the original
        # checkpoint even if its first attempt already appended this input.
        checkpoint = await workflow.execute_activity('native_agent_admission_v1', scope, result_type=dict,
            task_queue=workflow.info().task_queue + '-control',
            start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=3))
        prepared = await workflow.execute_activity('native_agent_prepare_v1', args=[scope, checkpoint], result_type=TurnInput,
            task_queue=workflow.info().task_queue + '-control',
            start_to_close_timeout=timedelta(minutes=5), retry_policy=RetryPolicy(maximum_attempts=3))
        services = Services(prepared)
        catalog = await services.invoke('native_agent_catalog_v1', prepared.scope)
        async def wait_for_model(pending):
            await workflow.wait_condition(
                lambda: pending.done() or bool(session.inbox) or session.result is not None)
        session = Session(services, prepared, wait_for_model=wait_for_model)
        agent = build_agent(session, catalog,
                            model=DurableSessionModel(model='moyai-native', session=session))

        async def inputs():
            seen = 0
            while True:
                await workflow.wait_condition(lambda: self.revision != seen)
                seen = self.revision
                await session.poll()

        async def computer():
            while True:
                await workflow.wait_condition(lambda: self.computer_requested)
                self.computer_requested = False
                await workflow.execute_activity('deepagents_computer_v1', scope,
                    task_queue=workflow.info().task_queue + '-tools',
                    start_to_close_timeout=timedelta(hours=12), heartbeat_timeout=timedelta(seconds=5),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    retry_policy=RetryPolicy(maximum_attempts=1))

        async def graph():
            result = {}
            async with aclosing(agent.astream({
                **prepared.native_state,
                'messages': [*messages_from_dict(prepared.messages), HumanMessage(content=prepared.prompt)],
            }, config={'recursion_limit': 1000}, stream_mode='values')) as stream:
                async for result in stream:
                    pass
            return result

        tasks = [asyncio.create_task(fn()) for fn in (graph, inputs, computer)]
        try:
            await workflow.wait_condition(lambda: any(task.done() for task in tasks))
            for task in tasks[1:]:
                if task.done():
                    await task
            state = await tasks[0]
            native_state = {key: state[key] for key in ('files', 'todos') if key in state}
            messages = messages_to_dict(state['messages'])
            await services.invoke('native_agent_checkpoint_v1', prepared.scope,
                                  prepared.native_lease, {**native_state, 'messages': messages})
            return TurnResult(session.result or {'completed': True, 'message': ''},
                              messages, session.applied, native_state)
        except Exception as exc:
            # Fail the execution, not an endlessly retried workflow task. The
            # codec encrypts failure details; public diagnostics use this type.
            raise ApplicationError('Native agent execution failed. Completed action receipts remain saved.',
                                   type='NativeAgentFailed', non_retryable=True) from exc
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
