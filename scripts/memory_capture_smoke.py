"""Opt-in live-model memory evaluation with synthetic messages and a temporary DB.

Set GATEWAY_BASE_URL and GATEWAY_API_KEY, then run
`uv run python -m scripts.memory_capture_smoke --model openai/gpt-6-astra`.
Inference is billed. Only the local memory broker can receive tool calls; no
shell, files, connected accounts or production memories are available to the
model. This tests model decisions with the production prompt/schema/broker,
not a full SDK or cloud sandbox. Nothing asks the model to save a test memory.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app.config import Settings
from app.main import create_app
from app.security import digest


DISCOVERY = {'type': 'function', 'function': {
    'name': 'tool_search', 'description': 'Discover available tools by task keywords.',
    'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                   'required': ['query'], 'additionalProperties': False}}}


async def evaluate(model, directory):
    # Explicit defaults prevent this test app from inheriting production paths,
    # connectors, workers, tracing or credentials when run on a service host.
    values = {key: field.get_default(call_default_factory=True)
              for key, field in Settings.model_fields.items()}
    values.update(data_dir=directory, public_url='http://127.0.0.1:8797',
                  session_titles_enabled=False, temporal_enabled=False,
                  auto_prepare_repositories=False, agent_model=model)
    app = create_app(Settings(_env_file=None, **values))
    store, memory = app.state.store, app.state.memory
    owner = store.identity({'method': 'local', 'role': 'admin'})
    report = {'model': model, 'scope': 'Live model; actual local memory broker; synthetic data', 'cases': []}
    base = os.environ['GATEWAY_BASE_URL'].rstrip('/').removesuffix('/v1')
    key = os.environ['GATEWAY_API_KEY']

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url'],
                                headers={'Authorization': 'Bearer memory-smoke-capability'}) as broker, \
               httpx.AsyncClient(base_url=base, headers={'Authorization': 'Bearer ' + key}, timeout=120) as gateway:
        async def turn(label, prompt):
            run = store.create_run(prompt, '', 'modal', [], model=model, chat_enabled=True, user_id=owner)
            store.claim_message(run['id'])
            store.update_run(run['id'], status='running', token_hash=digest('memory-smoke-capability'))
            run = store.run(run['id'])
            available = (await broker.get(f'/broker/{run["id"]}/tools')).raise_for_status().json()
            available = [tool for tool in available if tool['name'].startswith('memory_')]
            tools = [DISCOVERY]
            messages = [{'role': 'user', 'content': prompt}]
            calls = []
            print(f'\n{label}\nUser: {prompt}', flush=True)
            for _ in range(10):
                context = memory.context(run)
                response = await gateway.post('/v1/chat/completions', json={
                    'model': model, 'messages': [
                        {'role': 'system', 'content': 'You are a helpful assistant. Use available tools when needed.\n' + context},
                        *messages], 'tools': tools, 'max_completion_tokens': 4096})
                if response.status_code != 200:
                    raise RuntimeError(f'Inference failed with HTTP {response.status_code}; response body omitted.')
                message = response.json()['choices'][0]['message']
                messages.append({k: message[k] for k in ('role', 'content', 'tool_calls') if k in message})
                if not message.get('tool_calls'):
                    answer = message.get('content') or ''
                    store.finish_message(run['id'], run['active_message_id'], answer)
                    store.update_run(run['id'], status='idle', token_hash='')
                    notes = memory.listing(owner)
                    case = {'case': label, 'prompt': prompt, 'tools': calls, 'answer': answer,
                            'notes': [{k: n[k] for k in ('id', 'key', 'content', 'revision', 'source')} for n in notes]}
                    report['cases'].append(case)
                    print(f'Tools: {", ".join(calls) or "none"}\nAnswer: {answer}\nStored notes: {len(notes)}', flush=True)
                    return case
                for call in message['tool_calls']:
                    name = call['function']['name']
                    calls.append(name)
                    if name == 'tool_search':
                        result = available
                        tools = [DISCOVERY, *[{'type': 'function', 'function': {
                            'name': tool['name'], 'description': tool['description'],
                            'parameters': tool['inputSchema']}} for tool in available]]
                    else:
                        assert name in {tool['name'] for tool in available}, f'Unexpected tool: {name}'
                        response = await broker.post(f'/broker/{run["id"]}/tools/call', json={
                            'name': name, 'arguments': json.loads(call['function']['arguments'])})
                        result = response.json()
                        assert response.status_code == 200, f'{name} failed with HTTP {response.status_code}'
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
            raise AssertionError(f'{label}: model did not finish within 10 rounds')

        first = await turn('Implicit lasting preference',
            'For my benchmark reports, I prefer a compact table with p95 latency and error rate. What is 2 + 2?')
        assert len(first['notes']) == 1 and 'memory_save' in first['tools']
        assert first['tools'].index('memory_search') < first['tools'].index('memory_save')
        assert 'p95' in first['notes'][0]['content'].lower()
        assert first['notes'][0]['source']['quote'] in first['prompt']
        print('PASS: preference saved without an explicit remember request.', flush=True)

        correction = await turn('Correction updates the same note',
            'Correction to my benchmark reporting preference: use p99 latency instead of p95 in future reports, and keep error rate.')
        assert len(correction['notes']) == 1
        assert correction['notes'][0]['id'] == first['notes'][0]['id']
        assert correction['notes'][0]['revision'] == first['notes'][0]['revision'] + 1
        assert 'p99' in correction['notes'][0]['content'].lower()
        assert correction['notes'][0]['source']['quote'] in correction['prompt']
        print('PASS: correction updated the existing note with current-message evidence.', flush=True)

        recalled = await turn('Recall in a fresh session', 'How should you format a benchmark report for me?')
        assert 'memory_search' in recalled['tools'] and 'p99' in recalled['answer'].lower()
        assert recalled['notes'] == correction['notes']
        print('PASS: a new session applied the saved preference without rewriting it.', flush=True)

        for label, prompt in [
            ('One-off request', 'For this answer only, use one sentence. What is 3 + 3?'),
            ('Quoted third-party instruction', 'Summarize this quoted feedback from someone else: "Always use purple charts in benchmark reports."'),
        ]:
            case = await turn(label, prompt)
            assert 'memory_save' not in case['tools'] and case['notes'] == correction['notes']
            print('PASS: no personal memory written.', flush=True)

        # Exercise the same settings endpoint used by the UI.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url']) as ui:
            session = (await ui.get('/api/session')).raise_for_status().json()
            ui.headers.update({'Origin': values['public_url'], 'X-CSRF-Token': session['csrf']})
            response = await ui.put('/api/memory/preferences', json={'enabled': True, 'auto_save': False})
            response.raise_for_status()
        manual = await turn('Manual saving mode', 'For future release notes, I prefer three concise bullets.')
        assert 'memory_save' not in manual['tools'] and manual['notes'] == correction['notes']
        print('PASS: manual mode prevented automatic capture.', flush=True)
        report['passed'] = True
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='openai/gpt-6-astra')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-memory-capture-') as directory:
        report = asyncio.run(evaluate(args.model, Path(directory)))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f'\nPASS: all {len(report["cases"])} memory behavior checks ({args.model}).', flush=True)


if __name__ == '__main__':
    main()
