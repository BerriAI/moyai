"""Opt-in live-model regression through the actual local Moyai HTTP application.

Run through MOYAI_E2E_RUNNER. Uses real Temporal, normal app startup/intake and the
configured model gateway. Saved service tokens are synthetic, and the prompts
forbid service calls: this tests discovery before an access-blocked answer, not
provider authentication. No model, broker, tool, or runtime is replaced.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
from temporalio.testing import WorkflowEnvironment

from app.config import Settings
from app.main import create_app


CASES = [
    {
        'id': 'missing-environment',
        'prompt': (
            'For the Nimbus deployment audit, NIMBUS_API_TOKEN is not set in the workspace. '
            'What do you need from me to unblock live verification? Do not change code, '
            'call Nimbus, or modify any resources. I only need the access blocker assessment.'),
        'requires_discovery': True,
    },
    {
        'id': 'prior-blocker',
        'prompt': (
            'An earlier audit reported: "Live verification is blocked: I need an authorized Nimbus token." '
            'What do you need from me to unblock yourself? Keep this review-only. '
            'Do not call Nimbus or create infrastructure.'),
        'requires_discovery': True,
    },
    {
        'id': 'explanation-only',
        'prompt': (
            'Explain in one sentence why a missing API-token environment variable does not '
            'necessarily mean credentials are unavailable. This is conceptual only: do not use tools, '
            'inspect saved credentials, or make network calls.'),
        'requires_discovery': False,
    },
]


async def wait_until(predicate, seconds):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.1)


async def run_case(api, store, case, model, trial, observations, require_skill):
    started = time.monotonic()
    response = await api.post('/api/runs', json={
        'prompt': case['prompt'], 'mode': 'modal', 'chat_enabled': True,
        'harness': 'deepagents-temporal', 'model': model, 'plugins': [],
        'environment_id': 'none', 'repo_url': '',
    })
    response.raise_for_status()
    run_id = response.json()['id']
    result = {'case': case['id'], 'trial': trial, 'session_id': run_id}
    try:
        await wait_until(lambda: store.run(run_id)['status'] in {
            'idle', 'completed', 'failed', 'cancelled', 'waiting_credential'}, 240)
    except TimeoutError:
        await api.post(f'/api/runs/{run_id}/cancel')
        result['timeout'] = True
    detail = await api.get(f'/api/runs/{run_id}')
    detail.raise_for_status()
    state = detail.json()
    # Read tool identity only; never copy credential command bodies or values.
    events = store.events(run_id, limit=10000)
    tools = [event['data']['tool'] for event in events
             if event['kind'] == 'tool' and event['data'].get('phase') == 'started']
    skill_reads = []
    for event in events:
        data = event['data']
        output = data.get('output')
        if (event['kind'] == 'tool' and data.get('tool') == 'bundled_skill_read'
                and data.get('phase') == 'completed' and isinstance(output, dict)
                and output.get('name') == 'access-discovery'
                and output.get('path') == 'SKILL.md' and output.get('offset') == 0
                and output.get('next_offset') is None and output.get('content')):
            skill_reads.append({'name': output['name'], 'revision': output['revision']})
    requests = store.rows('SELECT status,provider,name FROM credential_requests WHERE run_id=?', (run_id,))
    result.update(status=state['status'], answer=state['summary'], tools=tools,
                  credential_requests=requests, model_calls=state['model_calls'],
                  seconds=round(time.monotonic() - started, 2), skill_reads=skill_reads)
    inventory_reads = observations.get(run_id, [])
    result['inventory_reads'] = inventory_reads
    # Merely invoking the tool is insufficient: an exact-name miss can still
    # cause a false blocker. Require retrieval of the seeded connection.
    discovered = any(read['found_saved_connection'] for read in inventory_reads)
    result['passed'] = bool(
        state['status'] in {'idle', 'completed'} and state['model_calls'] > 0
        and not any(row['status'] == 'pending' for row in requests)
        and (discovered if case['requires_discovery'] else not tools and state['model_calls'] == 1)
        and (not require_skill or not case['requires_discovery'] or bool(skill_reads))
        and not state['sandbox_id']
    )
    return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    if not os.environ.get('LITELLM_API_KEY'):
        raise SystemExit('Run with the approved E2E launcher and a configured dev gateway.')
    report = {
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'arm': args.arm,
        'scope': 'Actual local Moyai HTTP app, real Temporal, live configured model; synthetic saved service token.',
        'model': args.model, 'cases': CASES, 'results': [],
        'access_skill_required': args.arm == 'after',
        'source_sha256': {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ('agent/prompts/system.md', 'agent/prompts/__init__.py',
                         'app/credentials.py', 'app/runner.py',
                         'agent/skills/access-discovery/SKILL.md')
            if (ROOT / path).is_file()
        },
    }
    def save():
        (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')

    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as temporal:
        settings = Settings(
            _env_file=None, data_dir=Path(os.environ['DATA_DIR']),
            public_url=args.public_url,
            temporal_enabled=True, temporal_address=temporal.client.service_client.config.target_host,
            temporal_namespace='default', temporal_api_key='', temporal_tls=False,
            temporal_task_queue='credential-behavior-' + args.arm,
            session_secret=secrets.token_hex(32),
            workspace_password=secrets.token_urlsafe(32),
            password_login_enabled=True, google_client_id='', google_client_secret='',
            agent_model=args.model, session_titles_enabled=False,
            memory_review_enabled=False, skill_learning_enabled=False,
            repository_context_enabled=False, litellm_spend_recovery_enabled=False,
            litellm_trace_api_key='', raindrop_write_key='', langfuse_secret_key='',
            langsmith_api_key='', braintrust_api_key='',
            run_timeout_seconds=300,
        )
        password_file = settings.data_dir / 'probe-login-password'
        password_file.write_text(settings.workspace_password)
        password_file.chmod(0o600)
        print(f'Isolated local login file: {password_file}', flush=True)
        app = create_app(settings)
        observations = {}
        seeded_id = ''
        credential_call = app.state.credentials.call

        async def observe_credentials(run, name, arguments):
            # Passive observation of the real broker; no result is substituted.
            result = await credential_call(run, name, arguments)
            if name == 'credentials_list':
                observations.setdefault(run['id'], []).append({
                    'provider_filter': arguments.get('provider'),
                    'name_filter': arguments.get('name', ''),
                    'found_saved_connection': any(row['id'] == seeded_id for row in result['credentials']),
                })
            return result

        app.state.credentials.call = observe_credentials
        server = uvicorn.Server(uvicorn.Config(
            app, host='127.0.0.1', port=args.port, log_level='error', access_log=False))
        serving = asyncio.create_task(server.serve())
        try:
            await wait_until(lambda: server.started or serving.done(), 75)
            if not server.started:
                await serving
                raise RuntimeError('Application startup failed')
            async with httpx.AsyncClient(base_url=settings.public_url, timeout=30,
                                         headers={'Origin': settings.public_url}) as api:
                response = await api.post('/api/login', json={'password': settings.workspace_password})
                response.raise_for_status()
                session = (await api.get('/api/session')).json()
                api.headers['X-CSRF-Token'] = session['csrf']
                response = await api.post('/api/credentials/secrets', json={
                    'provider': 'generic', 'name': 'nimbus-audit', 'label': 'Nimbus audit access',
                    'format': 'env', 'scope': 'organization', 'lifetime': 'persistent',
                    'value': json.dumps({'NIMBUS_API_TOKEN': 'synthetic-discovery-input-not-a-real-service-key'}),
                    'client_id': 'nimbus-probe-seed',
                })
                response.raise_for_status()
                seeded_id = response.json()['id']
                for case in CASES:
                    for trial in range(1, (args.trials if case['requires_discovery'] else 1) + 1):
                        result = await run_case(api, app.state.store, case, args.model, trial,
                                                observations, args.arm == 'after')
                        report['results'].append(result)
                        save()
                        print(json.dumps({k: v for k, v in result.items() if k != 'answer'}), flush=True)
                report['passed'] = all(result['passed'] for result in report['results'])
                report['demo_url'] = settings.public_url
                save()
                print(f'Local application ready for inspection at {settings.public_url}', flush=True)
                if args.serve:
                    await serving
        finally:
            server.should_exit = True
            await serving
            app.state.store.close()
            save()
    if not report.get('passed'):
        raise SystemExit('Credential discovery regression failed; inspect results.json.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=['before', 'after'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8893)
    parser.add_argument('--public-url', required=True, help='Temporary HTTPS tunnel to this authenticated local app')
    parser.add_argument('--model', default='openai/gpt-6-astra-ultrafast')
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials must be positive')
    asyncio.run(main(args))
