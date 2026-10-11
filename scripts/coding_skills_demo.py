"""Local coding-pack demo using real library, discovery and load APIs.

uv run --frozen python scripts/coding_skills_demo.py --port 8843
Open /demo/login for the actual Skills UI; /demo/probe shows real context output.
Uses disposable data, no model call, background service or cloud sandbox. This
demonstrates the loading contract, not model quality or automatic skill selection.
"""
import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
import uvicorn

from app.config import Settings
from app.db import database
from app.main import create_app


class Probe(BaseModel):
    stage: str = 'directory'


def build(directory, port):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url=f'http://127.0.0.1:{port}',
                  google_client_id='local-demo', google_client_secret='local-demo',
                  google_allowed_domains='example.com', google_admin_emails='alex@example.com',
                  session_secret='local-demo-only', auto_prepare_repositories=False,
                  session_titles_enabled=False, memory_review_enabled=False)
    app = create_app(Settings(_env_file=None, **values))
    store, skills = app.state.store, app.state.skills
    owner = store.identity({'method': 'google', 'identity': {'sub': 'alex', 'email': 'alex@example.com', 'name': 'Alex'}})

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            store.close()
    app.router.lifespan_context = lifespan

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/#skills')
        app.state.security.new_session(response, identity={'sub': 'alex', 'email': 'alex@example.com',
                                                        'domain': 'example.com', 'name': 'Alex'})
        return response

    @app.post('/demo/context')
    async def context(body: Probe):
        def inspect():
            run = store.create_run('Fix Bugbot findings and failing CI on this PR', 'https://github.com/BerriAI/moyai',
                                   'demo', [], chat_enabled=True, user_id=owner)
            store.claim_message(run['id'])
            run = store.run(run['id'])
            if body.stage in {'instructions', 'reference'}:
                skills.call(run, 'skills_load', {'name': 'org:pr-feedback-loop'})
            if body.stage == 'reference':
                skills.call(run, 'skills_read_file', {'name': 'org:pr-feedback-loop', 'path': 'references/ci-and-reviews.md'})
            payload = json.loads(skills.context(run).split('\n', 1)[1])
            store.finish_message(run['id'], run['active_message_id'], 'Local loading verification; no model was called.')
            store.update_run(run['id'], status='idle')
            return {'directory_characters': len(json.dumps(payload['available'], ensure_ascii=False)),
                    'loaded_skills': len(payload['loaded']),
                    'loaded_references': sum(len(s['excerpts']) for s in payload['loaded']),
                    'model_calls': len(store.rows('SELECT id FROM model_requests')),
                    'sandbox_runs': len(store.rows("SELECT id FROM runs WHERE sandbox_id!=''")), 'context': payload}
        return await database(inspect)

    @app.get('/demo/probe', response_class=HTMLResponse)
    async def probe():
        return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Moyai coding skills · local verification</title><style>
body{margin:36px auto;max-width:1050px;padding:0 24px;background:#0c1421;color:#edf3fa;font:16px system-ui;line-height:1.5}
h1{font-size:30px}p{color:#b4c5d9}button,a{font:inherit;margin:4px 10px 8px 0;padding:10px 16px;border-radius:8px;border:1px solid #58718e;background:#17283b;color:#edf3fa;cursor:pointer}
button:focus-visible{outline:3px solid #99d7ff}#summary{padding:16px;border:1px solid #58718e;border-radius:8px;margin:16px 0;font-size:19px}
pre{background:#111f30;padding:20px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font:13px ui-monospace;max-height:560px;overflow:auto}
</style><h1>Moyai coding skills</h1><p>Real library and context APIs · disposable local data · no model or sandbox</p>
<p>Request: “Fix Bugbot findings and failing CI on this PR”</p>
<button data-stage="directory">1. Discover skills</button><button data-stage="instructions">2. Load PR workflow</button><button data-stage="reference">3. Read CI reference</button>
<div id="summary" role="status">Choose a step to inspect what the agent receives.</div><pre id="result"></pre>
<p>This demonstrates on-demand loading. It does not measure model quality or prove automatic selection.</p>
<script src="/demo/probe.js"></script></html>'''

    @app.get('/demo/probe.js')
    async def probe_script():
        # Keep the app's normal self-only script policy intact.
        return Response('''document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{
 document.getElementById('summary').textContent='Loading…';
 try{const r=await fetch('/demo/context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:b.dataset.stage})});if(!r.ok)throw Error('Request failed: '+r.status);
 const d=await r.json();document.getElementById('summary').textContent=`${d.loaded_skills} workflow loaded · ${d.loaded_references} reference loaded · ${d.model_calls} model calls · ${d.sandbox_runs} sandbox runs`;
 document.getElementById('result').textContent=JSON.stringify(d,null,2);
 }catch(e){document.getElementById('summary').textContent=e.message;}});''', media_type='application/javascript')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8843)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='coding-skills-demo-') as directory:
        print(f'Local demo: http://127.0.0.1:{args.port}/demo/login', flush=True)
        uvicorn.run(build(directory, args.port), host='127.0.0.1', port=args.port)
