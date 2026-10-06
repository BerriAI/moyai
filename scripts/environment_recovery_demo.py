"""Local app demo: uv run python scripts/environment_recovery_demo.py.

Open http://127.0.0.1:8827/demo#automations. Environment failure/recovery is
simulated; the inbox, launch checks, database and product UI are real. No
Modal, model, GitHub or Slack requests are made. Sessions stop at dispatch.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app.automations import Save
from app.config import Settings
from app.environments import Recipe, SaveRecipe
from app.main import create_app, STATIC
from app.temporal_runtime import TemporalRunManager


CONTROLS = '''<aside style="position:fixed;bottom:0;left:0;right:0;z-index:1000;background:#29243c;color:white;padding:16px 24px;display:flex;align-items:center;gap:20px;font:14px system-ui">
<div><strong>LOCAL VERIFICATION</strong><br>Environment outcomes simulated. Real inbox, launch checks and app UI.</div>
<button style="color:#29243c" id="demo-recheck">Recheck queued event</button><button style="color:#29243c" id="demo-repair">Simulate successful rebuild</button>
<strong id="demo-result">1 queued event · 0 sessions created</strong></aside>
<script src="/demo-controls.js" defer></script>'''

SCRIPT = '''
for(const [id,action] of [['demo-recheck','recheck'],['demo-repair','repair']]){
  document.getElementById(id).onclick=async()=>{
    const button=document.getElementById(id);button.disabled=true;
    try{
      const result=await api('/demo/'+action,{method:'POST',body:'{}'});
      await renderAutomations();
      document.getElementById('demo-result').textContent=result.pending+' queued events · '+result.sessions+' sessions created';
    }catch(error){document.getElementById('demo-result').textContent=error.message;}
    finally{button.disabled=false;}
  };
}
'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8827', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    service, store, env = app.state.automations, app.state.store, app.state.environments
    settings = app.state.settings
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_):
        # Readiness placeholders only; no provider workers are started.
        settings.modal_token_id = settings.modal_token_secret = settings.litellm_api_key = 'local-placeholder'
        settings.litellm_api_base = 'https://unused.invalid/v1'
        TemporalRunManager(store, settings)
        settings.temporal_enabled = True
        app.state.manager.ready = asyncio.Event()
        app.state.manager.ready.set()
        owner = store.identity({'method': 'local', 'role': 'admin'})
        env.save_recipe('e' * 32, SaveRecipe(recipe=Recipe(name='Moyai development', repository='BerriAI/moyai-devin', verify='true')), 'Local demo')
        store.execute('UPDATE environments SET activate_on_ready=1')
        build = env.enqueue('e' * 32, 1, 'Local demo')
        env.update(build['id'], phase='failed', error='Simulated Git authentication failure')
        saved = service.save(Save.model_validate({'definition': {
            'name': 'Complaint triage · local verification',
            'prompt': 'Investigate incoming complaints for Moyai Devin.',
            'mode': 'modal', 'repo_url': 'https://github.com/BerriAI/moyai-devin',
            'triggers': [{'id': 'complaints', 'event': {'provider': 'webhook', 'event': 'complaint.received'}}]
        }}), owner)
        store.execute('UPDATE automations SET paused=0,synced_revision=revision WHERE id=?', (saved['id'],))
        store.execute('INSERT INTO automation_webhooks VALUES(?,?,?)',
                      (saved['id'], 'webhook', app.state.security.encrypt('local-demo-only')))
        row = service.row(saved['id'])
        await service.events.accept(row, 'local-complaint-1', {'provider': 'webhook', 'event': 'complaint.received', 'body': 'A test complaint'})
        await service.events.dispatch()
        yield

    app.router.lifespan_context = lifespan

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return (STATIC / 'index.html').read_text().replace('</body>', CONTROLS + '</body>')

    @app.get('/demo-controls.js')
    async def controls():
        return Response(SCRIPT, media_type='text/javascript')

    @app.post('/demo/{action}')
    async def action(action: str, request: Request):
        app.state.security.require(request, admin=True, mutation=True)
        async with lock:
            if action == 'repair':
                build = env.enqueue('e' * 32, 1, 'Local demo')
                env.update(build['id'], phase='ready', snapshot_id='local-simulated-snapshot', commit_sha='a' * 40)
                store.execute('UPDATE environments SET enabled=1,activate_on_ready=0,active_build=?', (build['id'],))
            await service.events.dispatch()
            return {'pending': len(store.rows("SELECT occurrence FROM automation_events WHERE status='pending'")),
                    'sessions': len(store.rows('SELECT id FROM runs'))}

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-environment-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8827, log_level='warning')
