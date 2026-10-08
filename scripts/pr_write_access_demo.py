"""Real session UI/auth/broker; controlled GitHub and in-process task runner.

Start: .venv/bin/python scripts/pr_write_access_demo.py --port 8794
Open http://127.0.0.1:8794/demo. No credentials or external network needed.
The demo depends on dev dependencies and the regression GitHub fixture.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app.config import Settings
from app.main import create_app
from app.security import digest
from test_github import select
from test_github_write_access import ExternalAPI, REQUEST, update_args, comment_args

PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>PR access local demo</title>
<link rel="stylesheet" href="/static/style.css"></head><body><main style="padding:32px;max-width:700px">
<h1>PR access: controlled local demo</h1>
<p>This uses the real chat UI, authenticated consent endpoint, message queue and broker. GitHub and task execution are controlled fixtures. No external PR is written.</p>
<p>Start a chat, allow access, then select Continue in chat and send the message. The fixture runner calls the real update and comment tools and displays their responses. Revoke access and send another message to observe refusal.</p>
<button id="start">Start demo chat</button><p id="error" role="alert"></p></main><script src="/demo.js"></script></body></html>'''
SCRIPT = '''document.querySelector('#start').onclick=async()=>{try{
 const session=await (await fetch('/api/session')).json();
 const response=await fetch('/demo/start',{method:'POST',headers:{'X-CSRF-Token':session.csrf}});
 const data=await response.json();if(!response.ok)throw Error(JSON.stringify(data));
 location.href='/#run='+data.id;
}catch(e){document.querySelector('#error').textContent=e.message;}};'''


def demo(directory, port=8794):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}', agent_harness='hermes')
    app = create_app(Settings(_env_file=None, **values))
    store, github = app.state.store, app.state.connectors.github
    patch = pytest.MonkeyPatch()
    github.save_app({'id': 123, 'slug': 'demo', 'pem': 'fixture-only', 'owner_id': 44})
    select(app)
    fake = ExternalAPI(github, patch)
    app.state.demo_github = fake
    jobs = set()
    capability = 'local-demo-broker-capability'
    original_lifespan = app.router.lifespan_context

    async def broker(rid, name, args):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=values['public_url']) as client:
            response = await client.post(f'/broker/{rid}/tools/call', headers={'Authorization': 'Bearer ' + capability},
                                         json={'name': name, 'arguments': args})
            return response.json()

    async def work(rid):
        message = store.claim_message(rid)
        if not message:
            return
        store.execute("UPDATE runs SET mode='modal',status='running',token_hash=? WHERE id=?", (digest(capability), rid))
        status = await broker(rid, REQUEST, {'repository_id': 101, 'number': 100})
        if status.get('status') == 'approved':
            args = update_args().model_dump()
            args.update(base_sha=fake.branch, request_key='demo-update-' + str(message['id']))
            # The controlled transport supports follow-up bases as well.
            update = await broker(rid, 'github_update_pull_request', args)
            comment = await broker(rid, 'github_comment_pull_request', {
                **comment_args().model_dump(), 'request_key': 'demo-comment-' + str(message['id'])})
            result = f"Controlled GitHub results through actual broker routes:\n\nUpdate: `{update}`\n\nComment: `{comment}`\n\nNo external service was contacted. You can now revoke access and send another message."
        else:
            result = f"PR write access is **{status.get('status', 'unavailable')}**. No write attempted.\n\nUse the PR access panel below. Approval applies only to this chat and requester; select Continue in chat and send to resume."
        store.finish_message(rid, message['id'], result)
        store.execute("UPDATE runs SET mode='demo',status='completed',token_hash='' WHERE id=?", (rid,))

    original_request = fake.request
    async def transport(method, path, **kwargs):
        if method == 'GET' and path.endswith('/git/commits/' + fake.next_commit):
            from test_github import TREE
            return {'tree': {'sha': TREE}}
        return await original_request(method, path, **kwargs)
    patch.setattr(github, 'request', transport)

    def submit(run):
        job = asyncio.create_task(work(run['id']))
        jobs.add(job)
        job.add_done_callback(jobs.discard)
    patch.setattr(app.state.manager, 'submit', submit)
    # Demo tasks use the real queue, but never start a cloud runner or title inference.
    patch.setattr(app.state.session_titles, 'schedule', lambda *args: None)

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            yield
            if jobs:
                await asyncio.gather(*jobs)
            patch.undo()
    app.router.lifespan_context = lifespan

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE

    @app.get('/demo.js')
    async def script():
        return Response(SCRIPT, media_type='text/javascript')

    @app.post('/demo/start')
    async def start(request: Request):
        app.state.security.require(request, mutation=True)
        actor = store.identity(app.state.security.session_info(request))
        run = store.create_run('Controlled local PR approval demo', '', 'demo', ['github'],
                               chat_enabled=True, user_id=actor, model=app.state.settings.agent_model)
        await work(run['id'])
        return {'id': run['id']}

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8794)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='pr-access-demo-', dir=ROOT / '.scratch' if (ROOT / '.scratch').exists() else ROOT) as directory:
        uvicorn.run(demo(directory, args.port), host='127.0.0.1', port=args.port, log_level='warning', timeout_graceful_shutdown=2)
