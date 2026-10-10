"""Local real-API demo; simulated login/provider, no cloud or external inference.

Run: uv run python scripts/astra_ultrafast_demo.py --port 8917
Open http://127.0.0.1:8917/demo/login. Select a model, start a chat, then sign out
and return to /demo/login to verify the saved choice. Data lasts until shutdown.
"""
import argparse
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.config import Settings
from app.context_budget import ModelContextLimits
from app.main import create_app
from app.model_selection import ASTRA
from app.security import digest


def demo(directory, port):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.pop('agent_harness')
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}',
                  google_client_id='local-fixture', google_client_secret='local-fixture',
                  google_allowed_domains='example.com', google_admin_emails='alex@example.com',
                  agent_model=ASTRA, modal_token_id='local-fixture', modal_token_secret='local-fixture',
                  litellm_api_base=f'http://127.0.0.1:{port}/demo-gateway/v1', litellm_api_key='local-fixture',
                  model_context_limits={ASTRA: ModelContextLimits(context_window=32000, max_input_tokens=32000, max_output_tokens=4096)},
                  auto_prepare_repositories=False, session_titles_enabled=False, memory_review_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    # Only this loopback-only demo bypasses cloud readiness. Execution below is
    # replaced completely, while model preferences and broker routing are real.
    app.state.security.local = False

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        response.headers['set-cookie'] = response.headers['set-cookie'].replace('; Secure', '')
        return response

    @app.post('/demo-gateway/v1/responses')
    async def provider(request: Request):
        body = await request.json()
        assert body['model'] == ASTRA
        return {'id': 'local-tier-receipt', 'status': 'completed', 'model': body['model'],
                'service_tier': body['service_tier'], 'usage': {'input_tokens': 12, 'output_tokens': 8}}

    async def cloud(run):
        token = secrets.token_urlsafe(24)
        store = app.state.store
        store.update_run(run['id'], status='running', token_hash=digest(token))
        async with httpx.AsyncClient(base_url=values['public_url']) as client:
            response = await client.post(f"/broker/{run['id']}/v1/responses",
                headers={'Authorization': 'Bearer ' + token}, json={'input': run['prompt'], 'stream': False})
            response.raise_for_status()
        receipt = response.json()
        label = next(item['name'] for item in app.state.settings.model_choices() if item['id'] == run['active_model'])
        summary = (f"### Local verification\n\n**Selected:** {label}\n\n"
                   f"**Gateway model:** `{receipt['model']}`\n\n"
                   f"**Processing tier sent:** `{receipt['service_tier']}`\n\n"
                   'The real Moyai broker sent this request to a local provider stub. '
                   'No external model or cloud machine was used.\n\n'
                   'Choose **New session** to see your saved default, or sign out and return to `/demo/login`.')
        store.update_run(run['id'], status='completed', summary=summary)
        return False

    app.state.manager.cloud = cloud
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8917)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='astra-ultrafast-', dir=Path(__file__).resolve().parents[2]) as directory:
        uvicorn.run(demo(directory, args.port), host='127.0.0.1', port=args.port)
