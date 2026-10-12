"""Opt-in, billed response-format probe through the local app and live gateway.

Requires an existing eval bundle with manifest.json and run_evals.py. Its prompts
and strict oracle are imported unchanged. Runs real Deep Agents/Temporal sessions
and an optional tool-free native Claude adapter control. No cloud computer needed.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from cryptography.fernet import Fernet
from dotenv import dotenv_values

from agent.activity import ActivityReporter
from agent.harnesses.harness_registry import create_agent
from agent.prompts import system_prompt
from app import harness_gateway
from app.config import Settings
from app.main import create_app
from app.security import digest
from sandbox.broker_relay import BrokerRelay
from test_deepagents_acceptance import until, web_worker

RULE = "Honor the user's requested response format exactly"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checks(case, answers, evaluator):
    try:
        if case['id'] == 'fenced-example':
            match = re.fullmatch(r'\s*```json\s*\n(.*?)\n```\s*', answers[-1], re.S)
            return bool(match and evaluator.equal_json(json.loads(match[1]), {'ready': True}))
        if case['id'] == 'switch-to-prose':
            return (evaluator.equal_json(json.loads(answers[0]), evaluator.CASES['L06']['oracle']['values'][0])
                    and all(term in answers[1] for term in ('Atlas', 'Mira', 'us-west'))
                    and any(date in answers[1] for date in ('2026-11-12', 'November 12, 2026', '12 November 2026'))
                    and not any(mark in answers[1] for mark in ('```', '{', '}')))
        return evaluator.verify(case, answers, {}, None)
    except json.JSONDecodeError:
        return False


async def deep_case(api, store, case, model, trial):
    started = time.monotonic()
    response = await api.post('/api/runs', json={
        'prompt': case['input']['prompt'], 'model': model, 'harness': 'deepagents-temporal',
        'mode': 'modal', 'plugins': [], 'environment_id': 'none', 'repo_url': '', 'chat_enabled': True})
    response.raise_for_status()
    run_id = response.json()['id']
    result = {'engine': 'deepagents-temporal', 'model': model, 'case': case['id'],
              'trial': trial, 'session_id': run_id, 'answers': [], 'completed': False}
    previous = set()
    try:
        for index, prompt in enumerate([None, *case['input']['followups']]):
            if index:
                response = await api.post(f'/api/runs/{run_id}/messages', json={
                    'content': prompt, 'send_now': False, 'client_id': secrets.token_hex(12)})
                response.raise_for_status()
            def settled():
                state = store.run(run_id)
                answers = [m for m in store.messages(run_id) if m['role'] == 'assistant'
                           and m['status'] == 'completed' and m['id'] not in previous]
                return state['status'] in {'failed', 'cancelled'} or (state['status'] == 'idle' and answers)
            await until(settled, seconds=180)
            response = await api.get('/api/runs/' + run_id)
            response.raise_for_status()
            state = response.json()
            assert state['model'] == model and state['harness'] == 'deepagents-temporal'
            assert state['status'] == 'idle', 'Session failed to settle successfully'
            assert not state['sandbox_id'], 'Text-only case unexpectedly requested a computer'
            answers = [m for m in state['messages'] if m['role'] == 'assistant'
                       and m['status'] == 'completed' and m['id'] not in previous]
            result['answers'].append(answers[-1]['content'])
            previous.update(m['id'] for m in state['messages'] if m['role'] == 'assistant')
        result['completed'] = True
    except Exception as exc:
        result['error'] = type(exc).__name__
        await api.post(f'/api/runs/{run_id}/cancel')
    result['seconds'] = round(time.monotonic() - started, 2)
    return result


def native_case(app, directory, case, model, trial):
    """Native SDK + sealed broker, without tools or a cloud session controller."""
    started = time.monotonic()
    store = app.state.store
    token = secrets.token_hex(24)
    old_token = os.environ.get('WORKSPACE_RUN_TOKEN')
    os.environ['WORKSPACE_RUN_TOKEN'] = token
    run = store.create_run(case['input']['prompt'], '', 'modal', [], model=model,
                           harness='claude-agent-sdk', chat_enabled=True, user_id='fixture')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest(token))
    relay = BrokerRelay(f'{app.state.settings.public_url}/broker/{run["id"]}', token).start()
    spec = {'model': model, 'harness': 'claude-agent-sdk', 'max_iterations': 3, 'timeout': 180,
            'independent_runtime': True}
    agent = create_agent('claude-agent-sdk', spec=spec, relay=relay,
        config={'mcp_servers': {'workspace': {'command': 'unused'}}},
        activity=ActivityReporter(lambda *args: None), step=lambda: None, cwd=str(directory))
    options = agent.options
    agent.options = lambda system: replace(options(system), tools=[], allowed_tools=[], mcp_servers={})
    system = system_prompt(spec, workspace=directory, session=directory)
    result = {'engine': 'claude-agent-sdk-tool-free', 'model': model, 'case': case['id'],
              'trial': trial, 'session_id': run['id'], 'answers': [], 'completed': False}
    try:
        history = []
        for prompt in [case['input']['prompt'], *case['input']['followups']]:
            response = agent.run_conversation(prompt, conversation_history=history, system_message=system)
            assert response['completed'], 'Native SDK did not complete'
            result['answers'].append(response['final_response'])
            history = response['messages']
        result['completed'] = True
    except Exception as exc:
        result['error'] = type(exc).__name__
    finally:
        agent.close()
        relay.close()
        store.update_run(run['id'], status='idle', token_hash='')
        if old_token is None:
            os.environ.pop('WORKSPACE_RUN_TOKEN', None)
        else:
            os.environ['WORKSPACE_RUN_TOKEN'] = old_token
    result['seconds'] = round(time.monotonic() - started, 2)
    return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('format_oracle', args.eval_dir / 'run_evals.py')
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    cases = [evaluator.CASES[name] for name in ('L05', 'L06', 'L07')]
    guards = [
        {'id': 'fenced-example', 'input': {'prompt':
            'Show a Markdown fenced JSON example containing exactly {"ready": true}. Include the json language label and no other prose.',
            'followups': []}},
        {'id': 'switch-to-prose', 'input': {'prompt': cases[1]['input']['prompt'], 'followups': [
            'Switch to prose now. Describe the full release plan in one plain sentence, including project, owner, region and date. Do not use JSON or a code block.']}}]
    key = os.environ.get('LITELLM_DEV_API_KEY') or (
        dotenv_values(args.env_file).get('LITELLM_DEV_API_KEY') if args.env_file else None)
    if not key:
        raise SystemExit('LITELLM_DEV_API_KEY is required (the production key is never used).')
    report = {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'gateway': args.gateway, 'manifest_sha256': sha(args.eval_dir / 'manifest.json'),
              'oracle_sha256': sha(args.eval_dir / 'run_evals.py'),
              'prompt_sha256': sha(ROOT / 'agent/prompts/system.md'),
              'cases': cases + guards, 'results': [], 'provider_requests': [],
              'scope': 'Local HTTP app, real Temporal and live provider. Native control uses SDK/relay with tools disabled.'}
    def save():
        (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    original = harness_gateway.authorized_payload
    def observed(body, route, model, context):
        payload = original(body, route, model, context)
        # Observe the actual serialized request after SDK conversion and broker
        # policy. Record no credential, headers, private reasoning or tool output.
        envelope = json.dumps({k: payload.get(k) for k in ('system', 'instructions', 'input')}, ensure_ascii=False)
        assert RULE in envelope, 'The format rule did not reach the provider request'
        report['provider_requests'].append({'route': route, 'model': model,
            'format_instruction_present': True, 'instruction_envelope_sha256': hashlib.sha256(envelope.encode()).hexdigest(),
            'tools': len(payload.get('tools', [])), 'case': active['case'], 'trial': active['trial']})
        return payload
    harness_gateway.authorized_payload = observed
    active = {}
    try:
        with tempfile.TemporaryDirectory(prefix='response-format-') as tmp:
            directory = Path(tmp)
            settings = Settings(_env_file=None, data_dir=directory / 'app', temporal_enabled=True,
                session_secret=secrets.token_hex(24), encryption_key=Fernet.generate_key().decode(),
                agent_model=args.model, session_titles_enabled=False,
                memory_review_enabled=False, skill_learning_enabled=False)
            app = create_app(settings)
            try:
                async with web_worker(app, activity_slots=2) as api:
                    settings.litellm_api_base, settings.litellm_api_key = args.gateway, key
                    arms = [('deepagents-temporal', args.model, args.trials)]
                    if args.native:
                        arms.append(('claude-agent-sdk-tool-free', args.model, args.trials))
                    if args.openai_model:
                        arms.append(('deepagents-temporal', args.openai_model, 1))
                    for engine, model, trials in arms:
                        for case in cases + guards:
                            for trial in range(1, (trials if case in cases else 1) + 1):
                                active.update(case=case['id'], trial=trial)
                                before = len(report['provider_requests'])
                                if engine == 'deepagents-temporal':
                                    result = await deep_case(api, app.state.store, case, model, trial)
                                else:
                                    result = await asyncio.to_thread(native_case, app, directory, case, model, trial)
                                result['provider_requests'] = len(report['provider_requests']) - before
                                result['passed'] = bool(result['completed'] and result['provider_requests']
                                                        and checks(case, result['answers'], evaluator))
                                report['results'].append(result)
                                save()
                                print(json.dumps({k: v for k, v in result.items() if k != 'answers'}), flush=True)
            finally:
                app.state.store.close()
    finally:
        harness_gateway.authorized_payload = original
        save()
    if not report['results'] or not all(row['passed'] for row in report['results']):
        raise SystemExit('One or more response-format cases failed; inspect raw saved responses.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
    parser.add_argument('--eval-dir', type=Path, required=True, help='Trusted existing manifest and oracle bundle')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--gateway', default='https://gateway-dev.litellm-sandbox.ai/v1')
    parser.add_argument('--model', default='anthropic/claude-opus-5-5')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--native', action='store_true', help='Add tool-free native Claude SDK control')
    parser.add_argument('--openai-model', help='Add one trial per case on Deep Agents with this OpenAI model')
    asyncio.run(main(parser.parse_args()))
