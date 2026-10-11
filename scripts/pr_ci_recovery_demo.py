"""Real local Moyai UI/connector demo with simulated GitHub and no model calls.

uv run --frozen python scripts/pr_ci_recovery_demo.py --port 8987
Open /demo, then start the demo. The server is localhost-only and disposable.
"""
import argparse
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import httpx
import pytest
import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings
from app.github import Publish, RulesetReviewers
from app.main import create_app
from app.security import digest
from agent.activity import ActivityReporter
from agent.harnesses.codex_harness import CodexAgent
from agent.harnesses.harness_agent import TurnJournal
from test_github import GitHubAPI, PAYLOAD, connected
from test_github_ci import CIProvider, SHA

IDENTITY = {'sub': 'pr-ci-demo', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'}


def demo(directory, port):
    defaults = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    defaults.update(data_dir=Path(directory), public_url=f'http://localhost:{port}',
        google_client_id='local-fixture', google_client_secret='local-fixture', google_allowed_domains='example.com',
        google_admin_emails=IDENTITY['email'], auto_prepare_repositories=False, session_titles_enabled=False)
    app = create_app(Settings(_env_file=None, **defaults))
    store, github = app.state.store, app.state.connectors.github
    owner = store.identity({'method': 'google', 'identity': IDENTITY})
    connected(app)
    tasks = set()

    async def perform(run, turn):
        try:
            await asyncio.sleep(6)
            with pytest.MonkeyPatch.context() as monkey:
                GitHubAPI(github, monkey)
                receipt = await github.publish(store.run(run['id']), Publish.model_validate(PAYLOAD))
            await asyncio.sleep(7)
            events = []
            def emit(kind, message, data):
                events.append((kind, message, data))
                store.event(run['id'], kind, message, data)
            reporter = ActivityReporter(emit)
            agent = CodexAgent(spec={'model': 'openai/gpt-6-astra'}, relay=SimpleNamespace(), config={},
                               activity=reporter, step=lambda: None, cwd=directory, definition=None)
            agent.journal = TurnJournal([], 'Read CI')
            agent.record_item({'type': 'tool_search_call', 'execution': 'client', 'call_id': 'search-demo',
                              'arguments': {'query': 'github checks workflow status'}}, completed=False)
            # Inject an additional formatter defect to verify the fallback,
            # independent of the fixed schema-type regression.
            with patch('agent.activity.tool_details', side_effect=TypeError('demo formatter failure')):
                agent.record_item({'type': 'tool_search_output', 'execution': 'client', 'call_id': 'search-demo',
                    'status': 'completed', 'tools': [{'type': 'function', 'name': 'github_update_ruleset_reviewers',
                                                     'parameters': RulesetReviewers.model_json_schema()}]}, completed=True)
            assert not agent.journal.pending and 'search-demo' in agent.completed
            provider, original = CIProvider(), httpx.AsyncClient
            with patch('app.github.httpx.AsyncClient', lambda **kwargs: original(
                    transport=httpx.MockTransport(provider.handle), **kwargs)), patch.object(github, 'app_jwt', return_value='demo-jwt'):
                async def call(name, **args):
                    reporter.start(name, name, args)
                    result = await github.call(store.run(run['id']), name, args)
                    reporter.complete(name, name, args, result)
                    return result
                checks = await call('github_ci_checks', head_sha=SHA)
                runs = await call('github_workflow_runs', head_sha=SHA)
                jobs = await call('github_workflow_jobs', run_id=runs['workflow_runs'][0]['id'])
                logs = await call('github_job_logs', job_id=jobs['jobs'][0]['id'])
            assert checks['check_runs'][0]['conclusion'] == 'failure' and '[redacted]' in logs['text']
            await asyncio.sleep(6)
            answer = (f"[PR #{receipt['number']}]({receipt['url']}) was created and announced before verification finished.\n\n"
                'The tool-search result was saved despite an injected formatting error, and execution continued. '
                'All four CI tools returned results: checks, workflow runs, jobs and a redacted job log. '
                'The sample test job failed; its result was reported without crashing this turn.\n\n'
                '**Local demo:** real Moyai application and connector code, simulated GitHub responses. '
                'No model calls or live repository writes.')
            store.finish_message(run['id'], turn['id'], answer)
            store.update_run(run['id'], status='idle', token_hash='')
        except Exception as exc:
            store.finish_message(run['id'], turn['id'], 'Local demo failed: ' + type(exc).__name__, status='failed')
            store.update_run(run['id'], status='failed', token_hash='')
            raise

    @app.get('/demo')
    async def controls():
        return HTMLResponse('<!doctype html><title>PR and CI recovery demo</title><main style="font:18px system-ui;max-width:700px;margin:80px auto">'
            '<h1>PR creation survives later errors</h1><p>Real local Moyai UI and connector code. Simulated GitHub. No model calls or live writes.</p>'
            '<p>The demo announces a PR after six seconds, injects a formatting failure, then continues through all four CI tools.</p>'
            '<form method="post" action="/demo/start"><button style="padding:12px 20px;font:inherit">Start local demo</button></form></main>')

    @app.post('/demo/start')
    async def start():
        run = store.create_run('Create the PR, verify CI, and preserve results if formatting fails.', '', 'modal', ['github'],
                               chat_enabled=True, user_id=owner)
        turn = store.claim_message(run['id'])
        store.update_run(run['id'], status='running', token_hash=digest('demo-only-capability'))
        store.event(run['id'], 'message', 'Local demo: I’m preparing the change using simulated GitHub responses.')
        store.event(run['id'], 'message', 'The change is ready to publish. Both ordinary progress slots are now used.')
        task = asyncio.create_task(perform(run, turn))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        response = RedirectResponse('/#run=' + run['id'], status_code=303)
        app.state.security.new_session(response, identity=IDENTITY)
        return response

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8987)
    options = parser.parse_args()
    with TemporaryDirectory(prefix='moyai-pr-ci-demo-') as directory:
        uvicorn.run(demo(directory, options.port), host='127.0.0.1', port=options.port)
