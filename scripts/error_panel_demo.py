"""Seed two local-only failure histories in the real Moyai app.

Run: uv run --frozen python scripts/error_panel_demo.py --port 8838
Open /demo/login?case=terminal or /demo/login?case=legacy on that localhost port.
The terminal fixture has an earlier recovered cancellation, nine successful model
responses, and a distinct terminal broker HTTP 500. The legacy fixture has the
recovered cancellation but no saved terminal diagnosis. These are seeded records,
not real model calls or evidence of a database outage. No production credentials,
background execution, broker requests, provider calls, or HTTP mutations are used.
"""
import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import JSONResponse, RedirectResponse
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
import uvicorn

from app.config import Settings
from app.deepagents_diagnostics import workflow_failure
from app.main import create_app


IDENTITY = {'sub': 'error-panel-demo', 'email': 'demo@example.com',
            'name': 'Local fixture', 'domain': 'example.com'}
EXPLANATION = ('The durable agent could not continue. Completed results are saved. '
               'Inspect saved action receipts before retrying any unconfirmed write.')


def seed(store, owner, case):
    title = 'Terminal broker failure after recovery' if case == 'terminal' else 'Legacy failure: recovered error only'
    run = store.create_run(f'Local diagnostic fixture: {title}. No external actions.', '', 'demo', [],
                           chat_enabled=True, user_id=owner, harness='deepagents-temporal', model='local-demo')
    message = store.claim_message(run['id'])
    store.event(run['id'], 'error', 'Earlier model cancellation (recovered fixture)', {
        'phase': 'broker_failure', 'exception_type': 'CancelledError', 'error_code': 'cancelled',
        'http_status': 200, 'request_id': 'recovered-model-request'}, turn_id=message['id'])
    for number in range(1, 10):
        store.event(run['id'], 'status', f'Fixture model response {number} completed after recovery.',
                    {'phase': 'model_response'}, turn_id=message['id'])
    if case == 'terminal':
        activity = ActivityError('Fixture catalog request failed.', scheduled_event_id=1, started_event_id=2,
            identity='local-fixture', activity_type='deepagents.invoke_tool', activity_id='catalog', retry_state=None)
        activity.__cause__ = ApplicationError('Fixture broker rejected the catalog request.', 500, type='BrokerRejected')
        store.event(run['id'], 'error', 'Durable agent workflow failed.',
                    {'phase': 'sdk_failure', **workflow_failure(WorkflowFailureError(cause=activity))},
                    turn_id=message['id'])
    store.finish_message(run['id'], message['id'], EXPLANATION, 'failed')
    store.update_run(run['id'], status='failed', summary=EXPLANATION, error=EXPLANATION)
    store.execute('UPDATE runs SET display_title=? WHERE id=?', (title, run['id']))
    return run['id']


def demo(directory, port=8838):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}',
        google_client_id='local-fixture', google_client_secret='local-fixture',
        google_allowed_domains='example.com', google_admin_emails='demo@example.com',
        session_secret='local-error-panel-fixture', agent_model='local-demo',
        litellm_api_base='http://127.0.0.1:1/v1', temporal_enabled=False,
        auto_prepare_repositories=False, session_titles_enabled=False, memory_review_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    owner = app.state.store.identity({'method': 'google', 'identity': IDENTITY})
    app.state.demo_runs = {case: seed(app.state.store, owner, case) for case in ('terminal', 'legacy')}

    @asynccontextmanager
    async def offline_lifespan(application):
        yield

    app.router.lifespan_context = offline_lifespan
    allowed = {'/', '/health', '/demo/login', '/api/session', '/api/config', '/api/organization',
               '/api/runs', '/api/session-folders', '/api/connections', '/api/environments',
               '/api/connections/slack/personal'}
    for run_id in app.state.demo_runs.values():
        allowed.update(f'/api/runs/{run_id}{suffix}' for suffix in ('', '/activity', '/events', '/side-chats'))

    @app.middleware('http')
    async def read_only(request, call_next):
        if request.method not in {'GET', 'HEAD'} or (
                request.url.path not in allowed and not request.url.path.startswith('/static/')):
            return JSONResponse({'detail': 'This local demo only allows reading seeded sessions.'}, status_code=403)
        return await call_next(request)

    @app.get('/demo/login')
    async def login(case: Literal['terminal', 'legacy'] = 'terminal'):
        response = RedirectResponse('/#run=' + app.state.demo_runs[case])
        app.state.security.new_session(response, identity=IDENTITY)
        return response

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8838)
    args = parser.parse_args()
    if not args.port:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            args.port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1] / '.data'
    root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='error-panel-', dir=root) as directory:
        app = demo(directory, args.port)
        for case, run_id in app.state.demo_runs.items():
            print(f'{case}: http://127.0.0.1:{args.port}/demo/login?case={case} (run {run_id})', flush=True)
        try:
            uvicorn.run(app, host='127.0.0.1', port=args.port, lifespan='off')
        finally:
            app.state.store.close()


if __name__ == '__main__':
    main()
