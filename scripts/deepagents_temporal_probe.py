"""Local recovery proof: real Temporal/Deep Agents, scripted model, real file write.

No production endpoints or credentials. The output includes a live progress record
for recording the actual run, plus a verifiable final receipt.
"""
import argparse
import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict
from temporalio import activity
from temporalio.client import Client
from temporalio.converter import DataConverter, DefaultFailureConverterWithEncodedAttributes
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker, Replayer

from app.blob_storage import ObjectStorage
from app.deepagents_context import RuntimeInterceptor, deepagents_plugin
from app.deepagents_workflow import DeepAgentsTurnWorkflow, TurnInput, TurnScope
from app.temporal_payloads import PrivatePayloads, PrivatePayloadCodec


class Proof:
    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.after_write, self.release = asyncio.Event(), asyncio.Event()
        self.started = time.monotonic()
        self.events = []

    def record(self, text):
        event = {'seconds': round(time.monotonic() - self.started, 2), 'message': text}
        self.events.append(event)
        print(f"{event['seconds']:6.2f}s  {text}", flush=True)
        value = {'events': self.events, 'file_writes': self.calls,
                 'file_contents': (self.output / 'note.txt').read_text() if (self.output / 'note.txt').exists() else ''}
        temporary = self.output / '.progress.json'
        temporary.write_text(json.dumps(value))
        temporary.replace(self.output / 'progress.json')

    @activity.defn(name='deepagents_prepare_v1')
    async def prepare(self, scope: TurnScope) -> TurnInput:
        return TurnInput(replace(scope, model='local-scripted-provider', input_budget=10000),
            'Write one line to note.txt, then report completion.', 'Use the workspace tool boundary.')

    @activity.defn(name='deepagents_boundary_v1')
    async def boundary(self, scope: TurnScope, applied: list[int], elapsed: float, answer: str | None) -> dict:
        return {}

    @activity.defn(name='deepagents_publish_v1')
    async def publish(self, scope: TurnScope, identity: str, text: str) -> None:
        self.record('Agent response: ' + text)

    async def catalog(self, scope):
        return [{'name': 'write_note', 'inputSchema': {'type': 'object'}}]

    async def model_request(self, scope, messages, settings, parameters):
        if any(isinstance(m, ToolMessage) for m in messages):
            self.after_write.set()
            await self.release.wait()
            return AIMessage(content='The file is saved. The write ran exactly once.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {'name': 'write_note', 'arguments_json': '{}'}, 'id': 'proof-write'}])

    async def call_tool(self, scope, call_id, name, arguments):
        assert name == 'write_note'
        self.calls += 1
        with (self.output / 'note.txt').open('a') as stream:
            stream.write('Durable work survived the worker restart.\n')
        self.record('Tool completed: one real file write. Temporal saves its result.')
        return {'result': {'saved': True}}

    def worker(self, client):
        return Worker(client, task_queue='moyai-local-proof', workflows=[DeepAgentsTurnWorkflow],
            activities=[self.prepare, self.boundary, self.publish], interceptors=[RuntimeInterceptor(self)],
            graceful_shutdown_timeout=timedelta(seconds=.1))


async def demonstrate(output, pause):
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'note.txt').exists():
        raise ValueError('Use a fresh output directory so the write count is independently verifiable.')
    proof = Proof(output)
    proof.record('Starting local proof: real Temporal and Deep Agents; scripted provider.')
    with tempfile.TemporaryDirectory(prefix='moyai-proof-') as data:
        payloads = PrivatePayloads(SimpleNamespace(objects=ObjectStorage()),
            SimpleNamespace(data_dir=Path(data), encryption_key=Fernet.generate_key().decode()))
        conversion = DataConverter(payload_codec=PrivatePayloadCodec(payloads),
                                   failure_converter_class=DefaultFailureConverterWithEncodedAttributes)
        async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
            config = env.client.config()
            config.update(data_converter=conversion, plugins=[deepagents_plugin()])
            client = Client(**config)
            first = proof.worker(client)
            task = asyncio.create_task(first.run())
            handle = await client.start_workflow(DeepAgentsTurnWorkflow.run, TurnScope('local-proof', 1),
                id='moyai-local-recovery-proof', task_queue='moyai-local-proof')
            try:
                await asyncio.wait_for(proof.after_write.wait(), 30)
                await asyncio.sleep(pause)
                proof.record('Stopping the worker while the next model request is in flight.')
            finally:
                await first.shutdown()
                await task
            proof.record('Worker offline. The completed file and Temporal history are preserved.')
            await asyncio.sleep(pause)
            proof.record('Starting a replacement worker from the saved workflow history.')
            proof.release.set()
            async with proof.worker(client):
                result = await asyncio.wait_for(handle.result(), 30)
            assert result.result['completed'] and proof.calls == 1
            raw = (output / 'note.txt').read_bytes()
            assert raw.count(b'\n') == 1
            history = await handle.fetch_history()
            assert 'Write one line' not in history.to_json()
            await Replayer(workflows=[DeepAgentsTurnWorkflow], data_converter=client.data_converter,
                           plugins=[deepagents_plugin()]).replay_workflow(history)
            proof.record('Verified: one write, resumed final answer, deterministic replay, private history.')
            (output / 'receipt.json').write_text(json.dumps({
                'completed': True, 'file_writes': proof.calls, 'file_sha256': hashlib.sha256(raw).hexdigest(),
                'history_replayed': True, 'plaintext_prompt_in_history': False,
                'provider': 'scripted local fixture', 'tool': 'real local filesystem write',
                'events': proof.events,
            }, indent=2))
            await asyncio.sleep(pause)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pause', type=float, default=0, help='Pause at observed boundaries for a real recording.')
    args = parser.parse_args()
    asyncio.run(demonstrate(args.output, args.pause))


if __name__ == '__main__':
    main()
