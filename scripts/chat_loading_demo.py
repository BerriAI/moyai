"""Real local SQLite/session APIs with synthetic history and controlled latency.

Run: uv run python scripts/chat_loading_demo.py --port 8877
Open the printed /demo/login link. No models or connected services are called.
Use --root /path/to/baseline to compare the same fixture with an older checkout.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8877)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--delay-scale', type=float, default=1)
args = parser.parse_args()
sys.path.insert(0, str(args.root.resolve()))

import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from scripts.session_ui_demo import demo

PROBE = r"""
const requests=[], started=performance.now();
const originalFetch=window.fetch;
window.fetch=async(...args)=>{const path=String(args[0]),start=performance.now()-started;const response=await originalFetch(...args);if(path.startsWith('/api/'))requests.push({path,start_ms:Math.round(start),end_ms:Math.round(performance.now()-started)});return response;};
window.addEventListener('DOMContentLoaded',()=>{
  const panel=document.createElement('aside');panel.id='loading-diagnostic';
  panel.style='position:fixed;right:16px;bottom:18px;z-index:99999;width:355px;max-width:calc(100vw - 32px);box-sizing:border-box;background:white;border:1px solid #7354ac;border-radius:10px;padding:14px;box-shadow:0 2px 8px #0001;font:13px/1.45 system-ui';
  panel.innerHTML='<b>Local performance verification</b><p>100 sessions, 60 agents, 500 saved tool events.<br>Real local API; controlled request delays.</p><button id="measure-toggles">Measure six group toggles</button><pre id="loading-metrics" style="font:12px/1.5 monospace;white-space:pre-wrap"></pre><details><summary>Request timings</summary><pre id="request-metrics" style="max-height:140px;overflow:auto;font:10px monospace"></pre></details>';
  document.body.append(panel);
  let transcriptAt=null,sidebarAt=null,firstReply=null,result='';
  const paint=()=>{
    if(!transcriptAt&&document.querySelector('.chat-message.assistant')){transcriptAt=Math.round(performance.now()-started);firstReply=document.querySelector('.chat-message.assistant');}
    if(!sidebarAt&&document.querySelector('[data-toggle-agents]'))sidebarAt=Math.round(performance.now()-started);
    document.querySelector('#loading-metrics').textContent='Conversation visible: '+(transcriptAt===null?'loading…':transcriptAt+' ms')+'\nSession list visible: '+(sidebarAt===null?'loading…':sidebarAt+' ms')+'\nHistory fetches: '+requests.filter(r=>r.path.includes('/activity?')).length+'\nOriginal reply retained: '+Boolean(firstReply?.isConnected)+'\n'+result;
    document.querySelector('#request-metrics').textContent=requests.map(r=>r.path.replace(/[a-f0-9]{32}/g,'{id}')+' '+r.start_ms+' → '+r.end_ms+' ms').join('\n');
  };
  setInterval(paint,100);
  document.querySelector('#measure-toggles').onclick=async()=>{
    const message=document.querySelector('.chat-message.assistant'),unrelated=document.querySelectorAll('[data-run]')[3],samples=[];
    for(let i=0;i<6;i++){
      const toggle=document.querySelector('[data-toggle-agents]');if(!toggle)return;
      const before=performance.now();toggle.click();samples.push(Math.round((performance.now()-before)*10)/10);
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    }
    result='Toggle handler (ms): '+samples.join(', ')+'\nUnchanged row retained: '+Boolean(unrelated?.isConnected)+'\nMessage retained: '+Boolean(message?.isConnected)+'\nMounted session rows: '+document.querySelectorAll('[data-run]').length;paint();
  };
});
"""


def build(directory):
    app = demo(directory, args.port)
    store = app.state.store
    owner = store.identity({'method': 'google', 'identity': {
        'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex'}})

    def session(title):
        run = store.create_run(title, '', 'demo', [], chat_enabled=True, user_id=owner)
        message = store.claim_message(run['id'])
        store.finish_message(run['id'], message['id'], 'Saved local verification conversation.')
        store.update_run(run['id'], status='idle')
        return run

    roots = [session(f'Investigate session {index + 1}') for index in range(99)]
    for root in roots[:20]:
        for index in range(3):
            child = session(f'Worker {index + 1}')
            store.execute('UPDATE runs SET parent_run_id=?,agent_label=? WHERE id=?',
                          (root['id'], f'Worker {index + 1}', child['id']))
    selected = store.create_run('Verify faster chat loading', '', 'demo', [], chat_enabled=True, user_id=owner)
    for turn in range(5):
        if turn:
            store.enqueue_message(selected['id'], f'Check behavior {turn + 1}', f'demo-{turn}', user_id=owner)
        message = store.claim_message(selected['id'])
        for index in range(100):
            store.event(selected['id'], 'tool', f'Inspect file {index + 1}', {
                'tool': 'read_file', 'phase': 'completed', 'output': 'Synthetic saved tool output. ' * 80})
        store.finish_message(selected['id'], message['id'],
                             f'**Verification {turn + 1} completed**\n\nThe saved reply is ready. Expand the work history to inspect its 100 tool events.')
        store.update_run(selected['id'], status='idle')

    # Keep relative-time labels stable while comparing DOM identity. Crossing a
    # minute boundary legitimately changes every fresh row's displayed text.
    stamp = datetime.now(timezone.utc) - timedelta(days=2)
    for index, row in enumerate(store.rows('SELECT id FROM runs ORDER BY created_at,id')):
        value = (stamp + timedelta(seconds=index)).isoformat()
        store.execute('UPDATE runs SET created_at=?,updated_at=? WHERE id=?', (value, value, row['id']))

    @app.get('/demo/performance-login')
    async def login():
        response = RedirectResponse('/demo/workspace#run=' + selected['id'])
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response

    @app.get('/demo/workspace')
    async def workspace():
        html = (args.root / 'app/static/index.html').read_text()
        return HTMLResponse(html.replace('<head>', '<head><script src="/demo/probe.js"></script>'))

    @app.get('/demo/probe.js')
    async def probe():
        return Response(PROBE, media_type='text/javascript')

    @app.middleware('http')
    async def latency(request, call_next):
        delay = {'/api/config': .5, '/api/organization': 1.8,
                 '/api/runs': 1.3, '/api/session-folders': 1.3}.get(request.url.path, 0)
        if request.url.path.startswith('/api/runs/') and request.url.path.count('/') == 3:
            delay = .25
        if delay:
            await asyncio.sleep(delay * max(0, args.delay_scale))
        return await call_next(request)

    print(f'Local synthetic demo: http://127.0.0.1:{args.port}/demo/performance-login', flush=True)
    print(f'Source checkout: {args.root.resolve()}', flush=True)
    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='chat-loading-') as directory:
        uvicorn.run(build(directory), host='127.0.0.1', port=args.port)
