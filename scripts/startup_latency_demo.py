"""Real local session API with explicit, controlled transport delays; no cloud/model calls.

Open http://127.0.0.1:8892/demo/startup-login. The diagnostic overlay is demo-only.
"""
import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from session_ui_demo import demo

PROBE = r'''
addEventListener('DOMContentLoaded', () => {
  const panel = document.createElement('aside');
  panel.setAttribute('aria-label', 'Local startup measurements');
  panel.style.cssText = 'position:fixed;left:12px;bottom:65px;z-index:10000;width:264px;max-width:calc(100vw - 36px);padding:16px;background:#fff;border:1px solid #c5bddc;border-radius:10px;font:13px/1.6 system-ui;box-shadow:0 4px 18px #0001;pointer-events:none';
  document.body.append(panel);
  let start = 0, visible = null, ready = null, acknowledged = null, sidebar = null;
  const time = n => n === null ? 'waiting' : `${Math.round(n)} ms`;
  function paint() {
    panel.innerHTML = `<strong>Local startup verification</strong><br>Real UI + local APIs; simulated execution.<br>Injected delays: create 3s · detail 2s · sidebar 8s.<hr>Message visible: <b>${time(visible)}</b><br>Create acknowledged: ${time(acknowledged)}<br>Conversation ready: <b>${time(ready)}</b><br>Sidebar refreshed: ${time(sidebar)}`;
  }
  paint();
  const fetchRequest = window.fetch;
  window.fetch = async (path, options = {}) => {
    const response = await fetchRequest(path, options);
    if (start && /^\/api\/runs(?:\?|$)/.test(path)) {
      if (options.method === 'POST') acknowledged = performance.now() - start;
      else if (acknowledged !== null && sidebar === null) sidebar = performance.now() - start;
      paint();
    }
    return response;
  };
  document.addEventListener('submit', event => {
    if (event.target.id !== 'task-form') return;
    start = performance.now(); visible = ready = acknowledged = sidebar = null; paint();
  }, true);
  const observe = () => {
    if (!start) return;
    const elapsed = performance.now() - start;
    if (visible === null && document.querySelector('.chat-message.user')) visible = elapsed;
    if (ready === null && document.querySelector('#followup')) ready = elapsed;
    paint();
  };
  // Observe the actual conversation only, so painting diagnostics cannot loop.
  new MutationObserver(observe).observe(document.querySelector('#content'), {childList:true,subtree:true});
  new MutationObserver(observe).observe(document.querySelector('#session-list'), {childList:true,subtree:true});
});
'''


def build(directory, port, frontend_root=None):
    app = demo(directory, port)
    submitted = False
    frontend_root = frontend_root or Path(__file__).resolve().parents[1] / 'app/static'
    from starlette.staticfiles import StaticFiles
    for route in app.routes:
        if getattr(route, 'path', '') == '/static':
            route.app = StaticFiles(directory=frontend_root)

    @app.get('/demo/startup-login')
    async def login():
        response = RedirectResponse('/demo/startup')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response

    @app.get('/demo/startup')
    async def workspace():
        html = (frontend_root / 'index.html').read_text()
        return HTMLResponse(html.replace('<head>', '<head><script src="/demo/startup.js"></script>'))

    @app.get('/demo/startup.js')
    async def probe():
        return Response(PROBE, media_type='text/javascript')

    @app.middleware('http')
    async def latency(request, call_next):
        nonlocal submitted
        path = request.url.path
        if path == '/api/runs' and request.method == 'POST':
            submitted = True
            await asyncio.sleep(3)
        elif path == '/api/runs' and submitted:
            await asyncio.sleep(8)
        elif path.startswith('/api/runs/') and path.count('/') == 3:
            await asyncio.sleep(2)
        return await call_next(request)

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8892)
    parser.add_argument('--frontend-root', type=Path, help='Optional baseline app/static directory')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='startup-ui-') as directory:
        uvicorn.run(build(directory, args.port, args.frontend_root), host='127.0.0.1', port=args.port)
