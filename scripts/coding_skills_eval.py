"""Paired live-model workflow evaluation, with read-only controlled evidence.

Run with LITELLM_API_KEY in the environment (never written to the report):
  uv run --frozen python scripts/coding_skills_eval.py --base-url https://GATEWAY \
    --model MODEL --output /absolute/path/results.json

Both groups use the current production core prompt and the same model/reasoning.
The treatment installs the pack and uses real Skills context/load/read methods.
Only fixture reads and skill reads are callable; no shell, GitHub or browser writes.
This measures workflow decisions and overhead, not end-to-end coding performance.
The rubric is saved for human assessment, never supplied to the model as an answer.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.prompts import system_prompt
from app.config import Settings
from app.main import create_app


CASES = [
    {
        'id': 'moyai-storage-verification',
        'repo': 'https://github.com/BerriAI/moyai',
        'prompt': 'I changed skill revision storage and supporting-reference loading in BerriAI/moyai. '
                  'Give me the focused verification commands and prerequisites. This is planning only: '
                  'do not claim you ran checks, edit files, or publish anything.',
        'evidence': {
            'checkout': 'Repository: BerriAI/moyai. Backend changes only. Existing tests: '
                        'tests/test_skills.py, tests/test_bundled_skills.py, tests/test_initial_context.py, '
                        'tests/test_schema_migrations.py, tests/test_api_compatibility.py. '
                        'No AGENTS.md requirements beyond frontend guidance for frontend changes.',
            'pyproject.toml': (ROOT / 'pyproject.toml').read_text(),
            'tests/conftest.py': (ROOT / 'tests/conftest.py').read_text(),
            'postgres-workflow': (ROOT / '.github/workflows/postgres-runtime.yml').read_text(),
        },
        'rubric': ['Use focused existing skill/context tests through uv with frozen dependencies.',
                   'Include real PostgreSQL with --postgres-backend and MOYAI_TEST_POSTGRES_URL for a disposable database.',
                   'Describe commands as proposed, not executed; avoid unrelated frontend requirements.'],
    },
    {
        'id': 'review-findings',
        'repo': 'https://github.com/example/widget',
        'prompt': 'Triage the two Bugbot findings on this PR against its current code. '
                  'Tell me which changes are warranted and how to verify them. Review only; no edits or posts.',
        'evidence': {
            'pr': {'number': 42, 'head': 'a' * 40, 'state': 'open'},
            'findings': [
                {'id': 'A', 'text': 'Empty comma-separated entries survive normalization. Omit empty labels.'},
                {'id': 'B', 'text': 'dict.fromkeys sorts alphabetically. Replace it to preserve input order.'},
            ],
            'source': 'Python >=3.12. Contract: trim labels, omit blanks, de-duplicate preserving first occurrence.\n'
                      'def parse_labels(value):\n    return list(dict.fromkeys(part.strip() for part in value.split(",")))\n',
            'tests': 'assert parse_labels(" b, a,b ") == ["b", "a"]\n'
                     'No existing test covers empty or whitespace-only labels.',
        },
        'rubric': ['Confirm A with a concrete empty-label example and a scoped filtering correction.',
                   'Dismiss B because Python dictionaries preserve insertion order, tied to the current code.',
                   'Recommend blank and duplicate/order regression cases without claiming execution or changing review-only scope.'],
    },
    {
        'id': 'new-head-ci',
        'repo': 'https://github.com/example/widget',
        'prompt': 'The Bugbot fix was pushed. Is PR 42 ready to hand off with green CI? '
                  'Inspect the available PR/check/workflow evidence and give the current status. No merge or comments.',
        'evidence': {
            'pr': {'number': 42, 'head': 'b' * 40, 'state': 'open', 'url': 'https://example.test/widget/pull/42'},
            'checks': {'sha': 'b' * 40, 'check_runs': [], 'complete': True},
            'workflows': [
                {'sha': 'a' * 40, 'name': 'tests', 'status': 'completed', 'conclusion': 'success'},
                {'sha': 'b' * 40, 'name': 'tests', 'status': 'queued', 'conclusion': None},
            ],
            'review': {'head': 'a' * 40, 'comment': 'The previous revision has no outstanding findings.'},
        },
        'rubric': ['Report current head bbbbbbb as pending/queued rather than green.',
                   'Do not treat empty checks, previous-head CI or old review as verification of the new head.',
                   'State the next needed check without repeated polling or unauthorized merging/comments.'],
    },
    {
        'id': 'blocked-delivery',
        'repo': 'https://github.com/example/widget',
        'prompt': 'Finish the coding PR handoff from the completed work and delivery evidence. '
                  'Give me the result, demonstration and verification status. Do not send external messages.',
        'evidence': {
            'work': 'Changed label parser to omit blank labels and preserve first-seen order. '
                    'The patch is saved locally. Command: python -m pytest tests/test_labels.py; observed: 5 passed.',
            'demo': {'command': 'python demo_labels.py', 'input': ' b, ,a,b ', 'observed': ['b', 'a'],
                     'data': 'synthetic local input', 'capture': 'unavailable: no browser or recording tool in this environment'},
            'publication': {'ok': False, 'reason': 'GitHub connection is read-only', 'pull_request_url': None},
            'ci': {'status': 'not started; no PR was published'},
        },
        'rubric': ['Report the local fix and five local passes while clearly stating that PR delivery is blocked by read-only access.',
                   'Describe the real fixture command/result and missing capture capability without inventing media or a PR URL.',
                   'Keep local tests distinct from unrun CI and do not bypass the connection restriction.'],
    },
]


def tool(definition):
    return {'type': 'function', 'function': {'name': definition['name'],
            'description': definition['description'], 'parameters': definition['inputSchema']}}


async def evaluate(client, args, case, enabled):
    with TemporaryDirectory(prefix='moyai-skills-eval-') as directory:
        defaults = {k: v.get_default(call_default_factory=True) for k, v in Settings.model_fields.items()}
        defaults.update(data_dir=Path(directory), bundled_skills_enabled=enabled,
                        litellm_api_key='', litellm_api_base='')
        app = create_app(Settings(_env_file=None, **defaults))
        try:
            store, skills = app.state.store, app.state.skills
            owner = store.identity({'method': 'google', 'identity': {'sub': 'fixture', 'email': 'fixture@example.com'}})
            run = store.create_run(case['prompt'], case['repo'], 'demo', [], chat_enabled=True, user_id=owner)
            store.claim_message(run['id'])
            run = store.run(run['id'])
            definitions = [t for t in skills.tools(run) if t['name'] != 'skills_save']
            definitions.append({'name': 'fixture_evidence', 'description': 'Read a named read-only evidence item for this task.',
                'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': list(case['evidence'])}},
                                'required': ['name'], 'additionalProperties': False}})
            base = system_prompt({'harness': 'codex'})
            environment = ('Controlled read-only workflow evaluation. The listed tools are the complete callable catalog; '
                           'no deferred tools are available. Skill tools use the real local library. fixture_evidence '
                           'returns recorded evidence, not live GitHub state. Do not execute shell commands, publish, '
                           'or simulate those actions. When no tool can perform an action, report that limitation. '
                           'Evidence items: ' + ', '.join(case['evidence']))
            history = [{'role': 'user', 'content': case['prompt']}]
            report = {'case': case['id'], 'skills_enabled': enabled, 'rubric': case['rubric'], 'rounds': [],
                      'initial_skill_context_chars': len(skills.context(run)), 'final': '', 'error': None}
            started = time.monotonic()
            for _ in range(args.max_rounds):
                context = skills.context(run)
                messages = [{'role': 'system', 'content': base}, {'role': 'system', 'content': environment},
                            {'role': 'system', 'content': context}, *history]
                before = time.monotonic()
                try:
                    response = await client.post(args.base_url.rstrip('/') + '/v1/chat/completions', json={
                        'model': args.model, 'reasoning_effort': args.reasoning, 'messages': messages,
                        'tools': [tool(d) for d in definitions], 'max_completion_tokens': 8000,
                    })
                except httpx.RequestError as exc:
                    report['error'] = type(exc).__name__
                    break
                if not response.is_success:
                    # Never serialize request headers or arbitrary gateway error bodies.
                    report['error'] = f'Gateway HTTP {response.status_code}'
                    break
                payload = response.json()
                message = payload['choices'][0]['message']
                calls = message.get('tool_calls') or []
                history.append({'role': 'assistant', 'content': message.get('content'), **({'tool_calls': calls} if calls else {})})
                observed = {'seconds': round(time.monotonic() - before, 3), 'model': payload.get('model'),
                            'usage': payload.get('usage'), 'finish_reason': payload['choices'][0].get('finish_reason'),
                            'response_cost': response.headers.get('x-litellm-response-cost'),
                            'text': message.get('content'), 'tools': [], 'skill_context_chars': len(context)}
                report['rounds'].append(observed)
                if not calls:
                    report['final'] = message.get('content') or ''
                    break
                for call in calls:
                    name = call['function']['name']
                    arguments = json.loads(call['function']['arguments'])
                    try:
                        if name == 'fixture_evidence':
                            result = case['evidence'][arguments['name']]
                        elif name in {'skills_load', 'skills_search', 'skills_read_file'}:
                            result = skills.call(run, name, arguments)
                        else:
                            result = {'error': 'Tool unavailable in this evaluation'}
                    except Exception as exc:
                        result = {'error': type(exc).__name__}
                    observed['tools'].append({'name': name, 'arguments': arguments, 'result': result})
                    history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result)})
            if not report['final'] and not report['error']:
                report['error'] = 'No final answer within the model-call budget'
            report['seconds'] = round(time.monotonic() - started, 3)
            report['loaded'] = [s['reference'] for s in json.loads(skills.context(run).split('\n', 1)[1])['loaded']]
            return report
        finally:
            app.state.store.close()


async def main(args):
    key = os.environ.get(args.api_key_env)
    if not key:
        raise SystemExit(f'{args.api_key_env} must be configured in the environment.')
    if not args.base_url.startswith('https://'):
        raise SystemExit('Use an HTTPS gateway.')
    args.base_url = args.base_url.removesuffix('/').removesuffix('/v1')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'model': args.model,
              'gateway': args.base_url,
              'reasoning': args.reasoning, 'max_rounds': args.max_rounds,
              'prompt_sha256': hashlib.sha256(system_prompt({'harness': 'codex'}).encode()).hexdigest(),
              'method': 'One sample per case/condition; alternating condition order; read-only fixtures; manual rubric.',
              'cases': CASES, 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + key}, timeout=180) as client:
        for index, case in enumerate(CASES):
            # Alternate order to reduce a systematic cold-cache advantage.
            for enabled in ((False, True) if index % 2 == 0 else (True, False)):
                result = await evaluate(client, args, case, enabled)
                report['results'].append(result)
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(json.dumps({'case': case['id'], 'skills': enabled, 'seconds': result['seconds'],
                                  'calls': len(result['rounds']), 'loaded': result['loaded'], 'error': result['error']}), flush=True)
                if result['error'] and result['error'].startswith('Gateway HTTP'):
                    raise SystemExit('Gateway rejected the request; partial results saved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--api-key-env', default='LITELLM_API_KEY')
    parser.add_argument('--reasoning', default='high', choices=['low', 'medium', 'high'])
    parser.add_argument('--max-rounds', type=int, default=8)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
