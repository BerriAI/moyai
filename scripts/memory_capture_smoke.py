"""Opt-in live-model memory evaluation with synthetic messages and a temporary DB.

Set GATEWAY_BASE_URL and GATEWAY_API_KEY, then run
`uv run python -m scripts.memory_capture_smoke --model openai/gpt-6-astra`.
Inference is billed. Uses the production memory prompt/schema/broker, synthetic
messages, a deterministic scratch-file cache fixture, and a fixed calculator.
The fixture really interleaves local file writes/reads; it does not establish a
production benchmark defect. No arbitrary shell/files, connected accounts or
production memories are available. This tests model decisions and save ordering
in this serial tool loop, not a full SDK/cloud sandbox or guaranteed capture. No user prompt
asks the model to save a memory.
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
PROBE_SCOPE = 'local benchmark cache fixture'
TASK_TOOLS = [{'name': name, 'description': description,
               'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
              for name, description in [
                  ('benchmark_cache_probe', 'Run the local benchmark cache fixture with two workers and report whether shared versus separate cache paths preserve each worker output. This uses temporary scratch files.'),
                  ('calculate_total', 'Calculate 17 * 19 + 23 and return the exact result.')]]


def cache_probe(directory):
    result = {'scope': PROBE_SCOPE, 'fixture': 'deterministic file-write interleaving; synthetic data'}
    with TemporaryDirectory(prefix='benchmark-probe-', dir=directory) as scratch:
        workers = ('worker-a', 'worker-b')
        for mode in ('shared', 'isolated'):
            paths = {worker: Path(scratch) / (mode + '-' + ('cache' if mode == 'shared' else worker)) for worker in workers}
            for worker, path in paths.items():
                path.write_text(worker)
            observed = {worker: path.read_text() for worker, path in paths.items()}
            result[mode] = {'observed': observed, 'expected': {worker: worker for worker in workers},
                            'correct': all(observed[worker] == worker for worker in workers)}
    assert not result['shared']['correct'] and result['isolated']['correct']
    return result


async def evaluate(model, directory, report_path=None):
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
    report = {'model': model, 'scope': 'Live model; actual local memory broker; synthetic data', 'cases': [], 'passed': False}
    base = os.environ['GATEWAY_BASE_URL'].rstrip('/').removesuffix('/v1')
    key = os.environ['GATEWAY_API_KEY']

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=values['public_url'],
                                headers={'Authorization': 'Bearer memory-smoke-capability'}) as broker, \
               httpx.AsyncClient(base_url=base, headers={'Authorization': 'Bearer ' + key}, timeout=120) as gateway:
        async def turn(label, prompt, task_tools=False):
            run = store.create_run(prompt, '', 'modal', [], model=model, chat_enabled=True, user_id=owner)
            store.claim_message(run['id'])
            store.update_run(run['id'], status='running', token_hash=digest('memory-smoke-capability'))
            run = store.run(run['id'])
            available = (await broker.get(f'/broker/{run["id"]}/tools')).raise_for_status().json()
            available = [tool for tool in available if tool['name'].startswith('memory_')]
            if task_tools:
                available += TASK_TOOLS
            tools = [DISCOVERY]
            messages = [{'role': 'user', 'content': prompt}]
            calls, steps = [], []
            print(f'\n{label}\nUser: {prompt}', flush=True)
            for round_index in range(10):
                context = memory.context(run)
                response = await gateway.post('/v1/chat/completions', json={
                    'model': model, 'messages': [
                        {'role': 'system', 'content': 'You are a helpful assistant. Use available tools when needed. '
                         'Complete all requested tool work before your final answer; do not end with a promise to use a tool.\n' + context},
                        *messages], 'tools': tools, 'max_completion_tokens': 4096})
                if response.status_code != 200:
                    raise RuntimeError(f'Inference failed with HTTP {response.status_code}; response body omitted.')
                choice = response.json()['choices'][0]
                if choice['finish_reason'] not in {'stop', 'tool_calls'}:
                    raise RuntimeError(f"Incomplete model response: {choice['finish_reason']}")
                message = choice['message']
                print(f"Round {round_index}: {choice['finish_reason']}; phase={message.get('phase', 'unspecified')}", flush=True)
                messages.append({k: message[k] for k in ('role', 'content', 'tool_calls') if k in message})
                if not message.get('tool_calls'):
                    answer = message.get('content') or ''
                    store.finish_message(run['id'], run['active_message_id'], answer)
                    store.update_run(run['id'], status='idle', token_hash='')
                    notes = memory.listing(owner)
                    case = {'case': label, 'prompt': prompt, 'tools': calls, 'steps': steps, 'answer': answer,
                            'notes': [{k: n[k] for k in ('id', 'key', 'content', 'revision', 'source')} for n in notes]}
                    report['cases'].append(case)
                    if report_path:
                        report_path.write_text(json.dumps(report, indent=2) + '\n')
                    print(f'Tools: {", ".join(calls) or "none"}\nAnswer: {answer}\nStored notes: {len(notes)}', flush=True)
                    return case
                for call in message['tool_calls']:
                    name = call['function']['name']
                    calls.append(name)
                    step = {'name': name, 'round': round_index, 'index': len(calls) - 1}
                    if name == 'tool_search':
                        result = available
                        tools = [DISCOVERY, *[{'type': 'function', 'function': {
                            'name': tool['name'], 'description': tool['description'],
                            'parameters': tool['inputSchema']}} for tool in available]]
                    elif task_tools and name in {'benchmark_cache_probe', 'calculate_total'}:
                        assert json.loads(call['function']['arguments']) == {}
                        result = cache_probe(directory) if name == 'benchmark_cache_probe' else {'total': 17 * 19 + 23}
                        step['result'] = result
                    else:
                        assert name in {tool['name'] for tool in available}, f'Unexpected tool: {name}'
                        response = await broker.post(f'/broker/{run["id"]}/tools/call', json={
                            'name': name, 'arguments': json.loads(call['function']['arguments'])})
                        result = response.json()
                        assert response.status_code == 200, f'{name} failed with HTTP {response.status_code}'
                        if name == 'memory_search':
                            step['loaded_ids'] = [match['id'] for match in result['matches']]
                        if name == 'memory_save':
                            note = next(n for n in memory.listing(owner) if n['id'] == result['id'])
                            step.update(source_type=note['source']['type'], memory_id=note['id'],
                                run_status=store.run(run['id'])['status'], message_status=store.rows(
                                    'SELECT status FROM messages WHERE id=?', (run['active_message_id'],))[0]['status'])
                    steps.append(step)
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
            raise AssertionError(f'{label}: model did not finish within 10 rounds')

        first = await turn('Implicit lasting preference',
            'For my benchmark reports, I prefer a compact table with p95 latency and error rate. '
            'We use the local benchmark cache fixture repeatedly to compare workers. Run its cache probe and diagnose the result; '
            'once that is done, use the calculation tool to compute 17 * 19 + 23.', task_tools=True)
        assert len(first['notes']) == 2 and 'memory_save' in first['tools']
        assert first['tools'].index('memory_search') < first['tools'].index('memory_save')
        preference = next(n for n in first['notes'] if n['source']['type'] == 'chat')
        observation = next(n for n in first['notes'] if n['source']['type'] == 'observation')
        assert 'p95' in preference['content'].lower() and preference['source']['quote'] in first['prompt']
        assert observation['source']['scope'].casefold() in observation['content'].casefold()
        saves = [s for s in first['steps'] if s['name'] == 'memory_save']
        probe = next(s for s in first['steps'] if s['name'] == 'benchmark_cache_probe')
        calculation = next((s for s in first['steps'] if s['name'] == 'calculate_total'), None)
        assert calculation is not None, 'Model ended before the requested calculation; task follow-through is unverified.'
        assert len(saves) == 2 and [s['source_type'] for s in saves] == ['chat', 'observation']
        assert saves[0]['index'] < probe['index'] < saves[1]['index'] < calculation['index']
        assert probe['round'] < saves[1]['round'] and calculation['result']['total'] == 346
        assert all(s['run_status'] == s['message_status'] == 'running' for s in saves)
        print('PASS: preference saved before the probe; observed lesson saved after its result and before calculation, while running.', flush=True)

        correction = await turn('Correction updates the same note',
            'Correction to my benchmark reporting preference: use p99 latency instead of p95 in future reports, and keep error rate.')
        assert len(correction['notes']) == 2
        corrected = next(n for n in correction['notes'] if n['id'] == preference['id'])
        assert corrected['revision'] == preference['revision'] + 1
        assert 'p99' in corrected['content'].lower() and corrected['source']['quote'] in correction['prompt']
        assert observation in correction['notes']
        print('PASS: correction updated the existing note with current-message evidence.', flush=True)

        recalled = await turn('Recall in a fresh session', 'How should you format a benchmark report for me?')
        assert 'memory_search' in recalled['tools'] and 'p99' in recalled['answer'].lower()
        assert recalled['notes'] == correction['notes']
        print('PASS: a new session applied the saved preference without rewriting it.', flush=True)

        recalled = await turn('Recall a discovered lesson in a fresh session',
            'Before I use the local benchmark cache fixture again, what setup detail should I check for its workers?')
        assert 'memory_search' in recalled['tools'] and 'cache' in recalled['answer'].lower()
        assert any(observation['id'] in step.get('loaded_ids', []) for step in recalled['steps'])
        assert any(word in recalled['answer'].lower() for word in ('isolated', 'separate', 'per-worker', 'per worker', 'unique'))
        assert recalled['notes'] == correction['notes']
        print('PASS: a new session applied the observed cache lesson without rewriting notes.', flush=True)

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
        report = asyncio.run(evaluate(args.model, Path(directory), args.report))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f'\nPASS: all {len(report["cases"])} memory behavior checks ({args.model}).', flush=True)


if __name__ == '__main__':
    main()
