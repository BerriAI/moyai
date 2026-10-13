"""Local Moyai UI + real SDK + real Temporal; only the model replies are scripted.

Run: uv run --frozen python -m scripts.native_agent_demo
Open http://127.0.0.1:8799/demo-login and send a Cloud session request.
No hosted inference, sandbox provisioning or external app writes are used.
"""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tiktoken

from cryptography.fernet import Fernet
from fastapi import Request
from fastapi.responses import RedirectResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

from app.config import Settings
from app.context_budget import counting_input
from app.main import create_app
from deepagents_provider import answer, response_stream
from test_deepagents_acceptance import web_worker


async def main():
    with TemporaryDirectory(prefix='moyai-native-demo-') as directory:
        settings = Settings(_env_file=None, data_dir=Path(directory), temporal_enabled=True,
            agent_harness='deepagents-native', agent_model='test-model',
            session_secret='local-native-sdk-demo', encryption_key=Fernet.generate_key().decode(),
            session_titles_enabled=False, memory_review_enabled=False, skill_learning_enabled=False,
            bundled_skills_enabled=False, litellm_trace_api_key='', langfuse_secret_key='',
            langsmith_api_key='', braintrust_api_key='', raindrop_write_key='',
            model_context_limits={'test-model': {'context_window': 100000,
                'max_input_tokens': 90000, 'max_output_tokens': 10000}})
        app = create_app(settings)
        parent_calls = 0
        delegate_finished = False
        encoding = tiktoken.get_encoding('o200k_base')

        async def count(payload, **kwargs):
            messages, _, _ = counting_input(payload)
            return len(encoding.encode(json.dumps(messages))), 'demo_serialized_tokens'
        app.state.context_budget.count = count

        @app.get('/demo-login')
        async def login(request: Request):
            if request.client.host not in {'127.0.0.1', '::1'}:
                return RedirectResponse('/')
            response = RedirectResponse('/')
            app.state.security.local = True
            try:
                app.state.security.new_session(response, identity={
                    'sub': 'fixture', 'email': 'fixture@berri.ai', 'domain': 'berri.ai', 'name': 'SDK demo'})
            finally:
                app.state.security.local = False
            return response

        @app.post('/fixture/v1/responses')
        async def provider(request: Request):
            nonlocal parent_calls, delegate_finished
            body = await request.json()
            offered = {tool.get('name', '') for tool in body.get('tools', [])}
            transcript = json.dumps(body.get('input', []))
            delegated = 'task' not in offered
            if delegated:
                assert 'moyai_history_read' in offered
                if 'function_call_output' not in transcript:
                    name, arguments = 'moyai_history_read', {'limit': 3}
                    text = None
                else:
                    delegate_finished = True
                    name = None
                    text = 'Verified: the typed Moyai history capability returned this conversation.'
            else:
                parent_calls += 1
                steps = [
                    ('write_todos', {'todos': [{'content': 'Verify the native SDK integration', 'status': 'in_progress'}]}),
                    ('write_file', {'file_path': '/demo.txt', 'content': 'Hello from native Deep Agents.'}),
                    ('read_file', {'file_path': '/demo.txt'}),
                    ('task', {'description': 'Read the Moyai history and verify the application capability works.',
                              'subagent_type': 'general-purpose'}),
                    ('write_todos', {'todos': [{'content': 'Verify the native SDK integration', 'status': 'completed'}]}),
                ]
                if parent_calls <= len(steps):
                    name, arguments = steps[parent_calls - 1]
                    assert name in offered
                    text = None
                else:
                    assert delegate_finished and 'Hello from native Deep Agents.' in transcript
                    name = None
                    text = ('Native Deep Agents SDK verified.\n\n'
                        '• Planned and completed the work with write_todos.\n'
                        '• Wrote and read /demo.txt in the conversation notebook.\n'
                        '• Delegated verification through the SDK’s task tool.\n'
                        '• The subagent read real Moyai history through a typed, authorized tool.\n\n'
                        'Verified content: Hello from native Deep Agents.\n\n'
                        'Local demo: the SDK, Temporal, broker and tools are real. Model replies are scripted.')
            identity = f'demo-{parent_calls}-{"child" if delegated else "parent"}'
            response = answer(text or '', identity=identity)
            if name:
                response['output'] = [{'type': 'function_call', 'id': 'fc_' + identity,
                    'call_id': 'call_' + identity, 'name': name, 'status': 'completed',
                    'arguments': json.dumps(arguments)}]
            async def stream():
                await asyncio.sleep(.6)
                for chunk in response_stream(response):
                    yield chunk
                    await asyncio.sleep(.025)
            return StreamingResponse(stream(), media_type='text/event-stream')

        try:
            async with web_worker(app, activity_slots=4, port=8799):
                settings.litellm_api_base = settings.public_url + '/fixture'
                print('Native SDK demo: http://127.0.0.1:8799/demo-login', flush=True)
                print('Choose Cloud session, then send: Verify native Deep Agents planning, notebook and delegation.', flush=True)
                await asyncio.Event().wait()
        finally:
            app.state.store.close()


if __name__ == '__main__':
    asyncio.run(main())
