"""Local UI regression verification, with real session APIs and demo-mode execution.

Run: uv run python scripts/session_ui_demo.py
Open http://127.0.0.1:8830/demo/login. Identities below are local fixtures, not SSO.
No model, cloud, or connected-app calls are made.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from app.config import Settings
from app.main import create_app


def demo(directory, port=8830):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}',
                  google_client_id='local-fixture', google_client_secret='local-fixture',
                  google_allowed_domains='example.com', google_admin_emails='alex@example.com',
                  auto_prepare_repositories=False, session_titles_enabled=False,
                  demo_step_seconds=0.01)
    app = create_app(Settings(_env_file=None, **values))
    store = app.state.store

    # Local fixture sign-in only; this does not exercise Google SSO.
    from fastapi.responses import RedirectResponse

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response
    teammate = store.identity({'method': 'google', 'identity': {
        'sub': 'demo-sam', 'email': 'sam@example.com', 'name': 'Sam'}})
    run = store.create_run('Local verification: sender labels', '', 'demo', [],
                           chat_enabled=True, user_id=teammate)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'This is saved local test data, not a model response.')
    store.update_run(run['id'], status='idle')
    return app


if __name__ == '__main__':
    from tempfile import TemporaryDirectory
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8830)
    args = parser.parse_args()
    if not args.port:
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            args.port = listener.getsockname()[1]
    with TemporaryDirectory(prefix='session-ui-', dir=Path(__file__).resolve().parents[2]) as directory:
        uvicorn.run(demo(directory, args.port), host='127.0.0.1', port=args.port)
