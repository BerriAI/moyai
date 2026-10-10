"""Local real review/save/load APIs with synthetic task history and model output.

uv run python scripts/skill_learning_demo.py --port 8842
Open /demo/login. No provider, cloud or connected-app requests are made.
The temporary database is removed when the server stops.
"""
import argparse
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings
from app.db import database
from app.main import create_app
from app.skill_learning import Preferences


def build(directory, port):
    values = {k:f.get_default(call_default_factory=True) for k,f in Settings.model_fields.items()}
    values.update(data_dir=Path(directory),public_url=f'http://127.0.0.1:{port}',
        google_client_id='local-demo',google_client_secret='local-demo',google_allowed_domains='example.com',
        google_admin_emails='alex@example.com',session_secret='local-demo-only',
        litellm_api_base='https://synthetic.invalid/v1',litellm_api_key='local-fixture',
        auto_prepare_repositories=False,session_titles_enabled=False,memory_review_enabled=False,skill_learning_idle_seconds=0)
    app = create_app(Settings(_env_file=None,**values))
    store, learning = app.state.store, app.state.skill_learning
    owner = store.identity({'method':'google','identity':{'sub':'alex','email':'alex@example.com','name':'Alex'}})
    learning.set_preferences(owner,Preferences(enabled=True))
    repository = 'https://github.com/example/project'
    request = 'Validate the benchmark report using offline fixtures. Check every case before summarizing failures.'
    run = store.create_run(request,repository,'modal',[],chat_enabled=True,user_id=owner)
    message = store.claim_message(run['id'])
    store.event(run['id'],'tool','Run command',{'tool':'terminal','category':'command','phase':'completed',
        'exit_code':0,'command':'uv run pytest tests/test_benchmark.py -q'})
    store.finish_message(run['id'],message['id'],'The sample offline benchmark check passed. This is synthetic demo history.')
    store.update_run(run['id'],status='idle')

    def respond(request):
        data = json.loads(json.loads(request.content)['messages'][1]['content'])
        source = data['sources'][0]
        item = {'name':'benchmark-fixture-review','description':'Validate benchmark reports for example/project against the offline fixtures.',
            'instructions':f'# Benchmark fixture review\n\nUse for benchmark reports in {repository}.\n\n1. Run `uv run pytest tests/test_benchmark.py -q`.\n2. Check that each report case has a matching fixture.\n3. Summarize failed cases and checks not run.\n\nKeep the check offline. A successful test command does not establish production readiness.',
            'reason':'Your completed benchmark task used an offline fixture check that can be reused for future reports.',
            'target_id':'','target_revision':0,'evidence':[{'message_id':source['message_id'],'quote':source['request'],
                'tool_ids':[c['id'] for c in source['commands']]}]}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'suggestions':[item]})}}]})

    @asynccontextmanager
    async def lifespan(_):
        # Exercise the real background review once, using a simulated model.
        # Do not start application services that could call external systems.
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            learning.client = client
            await learning.process_next()
            try:
                yield
            finally:
                store.close()
    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def local_banner(request, call_next):
        if request.url.path == '/':
            page = (Path(__file__).resolve().parents[1]/'app/static/index.html').read_text()
            banner = '<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#111c30;color:#d8e8fa;padding:8px;text-align:center;font:12px system-ui">Local demo · sample task + simulated model suggestion · real review, save and load APIs</div>'
            return HTMLResponse(page.replace('</body>',banner+'</body>'))
        return await call_next(request)

    @app.get('/demo/login')
    async def login():
        response = RedirectResponse('/#skills')
        app.state.security.new_session(response,identity={'sub':'alex','email':'alex@example.com','domain':'example.com','name':'Alex'})
        return response

    @app.get('/demo/recall')
    async def recall():
        """Inspect the accepted skill through the real broker context path."""
        def context():
            run = store.create_run('/personal:benchmark-fixture-review Review the report',repository,'demo',[],chat_enabled=True,user_id=owner)
            store.claim_message(run['id'])
            text = app.state.skills.context(store.run(run['id']))
            store.finish_message(run['id'],store.run(run['id'])['active_message_id'],'Local context verification complete.')
            store.update_run(run['id'],status='idle')
            return {'loaded': 'Keep the check offline.' in text,'context':text}
        return await database(context)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8842)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='skill-learning-demo-') as directory:
        print(f'Local demo: http://127.0.0.1:{args.port}/demo/login',flush=True)
        uvicorn.run(build(directory,args.port),host='127.0.0.1',port=args.port)
