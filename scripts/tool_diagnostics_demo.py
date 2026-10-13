"""Execute a local tool failure and inspect the persisted OTLP diagnostic.

Run: uv run --frozen python scripts/tool_diagnostics_demo.py --output /tmp/tool-diagnostics
Uses the real runtime, database, failure projection and trace outbox. The broker
transport injects a timeout; there is no provider call or production replay.
Copy this script to the unpatched base and add --legacy for a before comparison.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from app.config import Settings
from app.deepagents_diagnostics import workflow_failure
from app.deepagents_workflow import TurnScope
from app.main import create_app
from app.security import digest
from app.tracing import AgentTracing


SENTINEL = 'synthetic-private-value-must-never-be-exported'


def scalar(value):
    kind = value.WhichOneof('value')
    if kind == 'array_value':
        return [scalar(item) for item in value.array_value.values]
    return getattr(value, kind) if kind else None


async def demonstrate(output, *, legacy=False, pause=0):
    output.mkdir(parents=True, exist_ok=True)

    async def say(text):
        print(text, flush=True)
        if pause:
            await asyncio.sleep(pause)

    await say('MOYAI / TOOL FAILURE DIAGNOSTICS — ' + ('BASELINE' if legacy else 'IMPROVED'))
    await say('Real runtime + database + OTLP. Local injected timeout; no production actions.')
    with TemporaryDirectory(prefix='moyai-tool-diagnostic-') as directory:
        values = {key: field.get_default(call_default_factory=True)
                  for key, field in Settings.model_fields.items()}
        values.update(data_dir=Path(directory), temporal_enabled=True,
                      session_secret='local-demo-session', agent_model='local-demo',
                      session_titles_enabled=False, memory_review_enabled=False)
        app = create_app(Settings(_env_file=None, **values))
        manager, store = app.state.manager, app.state.store
        trace_values = dict(values)
        trace_values.update(litellm_trace_endpoint='https://local-trace.invalid/v1/traces',
                            litellm_trace_api_key='local-demo-trace-key',
                            trace_environment='development')
        tracing = store.tracing = AgentTracing(store, Settings(_env_file=None, **trace_values))
        try:
            owner = store.identity({'method': 'google', 'identity': {
                'sub': 'diagnostic-demo', 'email': 'demo@example.com'}})
            run = store.create_run('Local tool diagnostic demonstration.', '', 'modal', [],
                                   chat_enabled=True, harness='deepagents-temporal', user_id=owner)
            manager.submit(run)
            manager.begin_turn(run['id'], store.run(run['id']))
            scope = TurnScope(run['id'], manager.state(run['id'])['message_id'], actor_id=owner)
            store.update_run(run['id'], status='running',
                             token_hash=digest(manager.token(run['id'], scope.message_id)))
            runtime = manager.deepagents_runtime
            runtime.journal.exchange(store.run(run['id']), {'action': 'import', 'entries': [],
                'manifest': {'id': 'empty', 'count': 0}, 'complete': True})
            attempted = []

            def broker(request):
                if request.url.path.endswith('/tools'):
                    return httpx.Response(200, json=[{'name': 'credentials_run'}])
                assert request.url.path.endswith('/agent-runtime')
                attempted.append(request.url.path)
                try:
                    raise TimeoutError(SENTINEL)
                except TimeoutError as cause:
                    raise httpx.ReadTimeout(SENTINEL, request=request) from cause

            runtime.broker_transport = httpx.MockTransport(broker)
            await say('\n1. Invoke credentials_run; inject a timeout while polling the computer.')
            try:
                await runtime._call_tool(scope, 'local-demo-call', 'credentials_run',
                                         {'command': SENTINEL, 'request_ids': []})
            except ApplicationError as failure:
                assert failure.type == 'UncertainToolOutcome' and failure.non_retryable
                diagnostic = workflow_failure(WorkflowFailureError(cause=failure))
            else:
                raise AssertionError('The injected timeout did not reach the runtime boundary')

            assert len(attempted) == 1
            store.event(run['id'], 'error', 'Durable agent workflow failed.',
                        {'phase': 'sdk_failure', **diagnostic}, turn_id=scope.message_id)
            store.finish_message(run['id'], scope.message_id,
                                 'Tool outcome unconfirmed; inspect the saved diagnostic.', 'failed')
            await say('   Outcome remains uncertain and non-retryable. One dispatch attempt.')
            await say('\n2. Export the saved tool and root spans through the actual outbox.')
            received = []

            def receiver(request):
                received.append(request.content)
                return httpx.Response(200)

            await tracing.outboxes[0].client.aclose()
            tracing.outboxes[0].client = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
            assert await tracing.outboxes[0].export_once()
            assert len(received) == 1 and SENTINEL.encode() not in received[0]
            assert b'local-demo-trace-key' not in received[0]
            encoded = ExportTraceServiceRequest.FromString(received[0])
            spans = [span for resource in encoded.resource_spans
                     for collection in resource.scope_spans for span in collection.spans]
            tool = next(span for span in spans if span.name == 'credentials_run')
            root = next(span for span in spans if span.name == 'moyai')
            tool_attributes = {item.key: scalar(item.value) for item in tool.attributes}
            root_attributes = {item.key: scalar(item.value) for item in root.attributes}
            assert tool.parent_span_id == root.span_id and tool.trace_id == root.trace_id
            assert tool.status.code == root.status.code == 2
            stage = root_attributes.get('moyai.error.failure_stage')
            if legacy:
                assert stage is None
            else:
                assert stage == 'computer_poll'
                assert root_attributes['error.type'] == tool_attributes['error.type'] == 'ReadTimeout'
                assert root_attributes.get('moyai.error.request_id')
                assert root_attributes.get('moyai.error.stack_locations')
            await say('   Both spans received. Same trace; tool is a child of the failed turn.')
            await say('\n3. Read the root diagnosis from the exported trace.')
            await say('   Exception: ' + str(root_attributes.get('error.type', 'unavailable')))
            await say('   Stage:     ' + str(stage or 'unavailable'))
            await say('   Chain:     ' + str(root_attributes.get('moyai.error.exception_chain', 'unavailable')))
            locations = root_attributes.get('moyai.error.stack_locations') or []
            for location in locations[-3:]:
                print('   Location:  ' + location, flush=True)
            await say('   Private command, exception text and trace key: absent from exported bytes.')
            proof = {
                'scope': 'Local fault injection with real runtime, database and OTLP outbox',
                'historical_root_cause_reproduced': False,
                'legacy': legacy, 'dispatch_attempts': len(attempted),
                'non_retryable': True, 'private_values_exported': False,
                'trace_id': root.trace_id.hex(), 'root_span_id': root.span_id.hex(),
                'tool_span_id': tool.span_id.hex(), 'spans_received': len(spans),
                'diagnostic': diagnostic,
                'root_error_attributes': {key: value for key, value in root_attributes.items()
                                          if key.startswith(('error.', 'moyai.error.'))},
                'tool_error_attributes': {key: value for key, value in tool_attributes.items()
                                          if key.startswith(('error.', 'moyai.error.'))},
            }
            (output / 'tool-diagnostics-proof.json').write_text(json.dumps(proof, indent=2) + '\n')
            (output / 'tool-diagnostics.otlp').write_bytes(received[0])
            await say('\nVERIFIED: ' + ('base hides the failing stage.' if legacy else
                                       'the failed tool is diagnosable without exposing its payload.'))
        finally:
            await tracing.close()
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--pause', type=float, default=0, choices=(0, 0.5, 1))
    args = parser.parse_args()
    asyncio.run(demonstrate(args.output, legacy=args.legacy, pause=args.pause))
