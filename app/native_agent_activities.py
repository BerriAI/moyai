"""Application I/O for the native graph. Every external action gets a receipt."""
import asyncio
from contextlib import asynccontextmanager
import hashlib

from langchain_core.messages import messages_from_dict, messages_to_dict
from temporalio import activity

from .deepagents_workflow import TurnInput, TurnScope
from .db import database

COMPATIBILITY = hashlib.sha256(b'moyai-native-v1:deepagents-0.7.15').hexdigest()


@asynccontextmanager
async def heartbeat():
    async def pulse():
        while True:
            activity.heartbeat()
            await asyncio.sleep(1)
    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class NativeActivities:
    def __init__(self, services):
        self.services = services

    @activity.defn(name='native_agent_activity_v1')
    async def report(self, scope: TurnScope, identity: str, name: str, phase: str, input_id: int) -> None:
        await database(self.services.native_activity, scope, identity, name, phase, input_id)

    @activity.defn(name='native_agent_prepare_v1')
    async def prepare(self, scope: TurnScope, checkpoint: dict) -> TurnInput:
        # Admission precedes the current user append, so the journal position
        # matches the previous completed turn. NativeSessions also fences the
        # actor, model, gateway, canonical input ledger and private-context taint.
        saved, lease = {}, ''
        if scope.actor_id:
            lease = hashlib.sha256(f'native:{scope.run_id}:{scope.message_id}:{scope.segment}'.encode()).hexdigest()[:32]
            saved = await self.services.broker(scope, '/context/native', {
                'action': 'begin', 'lease': lease, 'compatibility': COMPATIBILITY,
                'checkpoint': checkpoint})
        prepared = await self.services.prepare(scope)
        if scope.segment == 0 and saved.get('state'):
            state = saved['state']
            prepared.messages = state.get('messages', prepared.messages)
            prepared.native_state = {key: state[key] for key in ('files', 'todos') if key in state}
        prepared.native_lease = lease
        return prepared

    @activity.defn(name='native_agent_admission_v1')
    async def admission(self, scope: TurnScope) -> dict:
        if not scope.actor_id:
            return {}
        journal = await self.services.broker(scope, '/context/journal', {'action': 'state'})
        return {key: journal[key] for key in ('epoch', 'seq')}

    @activity.defn(name='native_agent_checkpoint_v1')
    async def checkpoint(self, scope: TurnScope, lease: str, state: dict) -> None:
        if not lease:
            return
        async with heartbeat():
            journal = await self.services.broker(scope, '/context/journal', {'action': 'state'})
            await self.services.broker(scope, '/context/native', {
                'action': 'commit', 'lease': lease, 'compatibility': COMPATIBILITY,
                'checkpoint': {key: journal[key] for key in ('epoch', 'seq')}, 'state': state})

    @activity.defn(name='native_agent_catalog_v1')
    async def catalog(self, scope: TurnScope) -> list[dict]:
        async with heartbeat():
            return await self.services.catalog(scope)

    @activity.defn(name='native_agent_model_v1')
    async def model(self, scope: TurnScope, messages: list[dict], settings: dict, tools: list[dict]) -> dict:
        async with heartbeat():
            result = await self.services.model_request(scope, messages_from_dict(messages), settings, tools)
            return messages_to_dict([result])[0]

    @activity.defn(name='native_agent_tool_v1')
    async def tool(self, scope: TurnScope, identity: str, name: str, arguments: dict) -> dict:
        async with heartbeat():
            return await self.services.call_tool(scope, identity, name, arguments)
