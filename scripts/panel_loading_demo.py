"""PR-panel performance fixture using real local APIs and synthetic GitHub reads.

Run: uv run python scripts/panel_loading_demo.py --port 8880
Use --source-root /path/to/baseline --port 8881 for the same fixture on old code.
No models, cloud workspaces, or external GitHub requests are used.
"""
import argparse
import asyncio
from contextvars import ContextVar
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8880)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--github-delay-ms', type=int, default=250)
args = parser.parse_args()
args.source_root = args.source_root.resolve(strict=True)
sys.path.insert(0, str(args.source_root))

import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from scripts.pull_request_panel_demo import IDENTITY, demo


PROBE = r"""
const reads=[];
const originalFetch=window.fetch;
window.fetch=async(...args)=>{
  const path=String(args[0]),started=performance.now();
  const response=await originalFetch(...args);
  if(path.includes('/pull-request?'))reads.push(Math.round(performance.now()-started));
  return response;
};
window.addEventListener('DOMContentLoaded',()=>{
  const panel=document.createElement('aside');panel.id='panel-diagnostic';
  panel.style='position:fixed;left:clamp(16px,calc(100vw - 600px),312px);bottom:20px;z-index:99999;width:345px;max-width:calc(100vw - 32px);box-sizing:border-box;background:white;border:1px solid #7354ac;border-radius:10px;padding:14px;box-shadow:0 2px 8px #0001;font:13px/1.45 system-ui';
  panel.innerHTML='<b>Local panel performance verification</b><p>40 files, 80 diff lines each.<br>Real local APIs; synthetic GitHub latency.</p><button id="measure-sections">Measure six section switches</button><pre id="panel-metrics" style="font:12px/1.5 monospace;white-space:pre-wrap"></pre>';
  document.body.append(panel);
  let samples=[],retained='not measured';
  const paint=()=>{
    const rows=document.querySelectorAll('.pr-diff tr').length;
    document.querySelector('#panel-metrics').textContent='PR request times: '+(reads.join(', ')||'not opened')+' ms\nMounted diff rows: '+rows+'\nSection clicks (ms): '+samples.join(', ')+'\nOriginal diff retained: '+retained;
  };
  document.querySelector('#measure-sections').onclick=async()=>{
    samples=[];const original=document.querySelector('.pr-diff');
    for(let i=0;i<6;i++){
      const button=document.querySelector('[data-section="'+(i%2?'changes':'description')+'"]');
      if(!button)return;
      const started=performance.now();button.click();samples.push(Math.round((performance.now()-started)*10)/10);
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    }
    retained=String(Boolean(original?.isConnected));paint();
  };
  setInterval(paint,100);
});
"""


def build(directory):
    app = demo(directory, args.port, static_root=args.source_root / 'app/static')
    github = app.state.connectors.github
    fixture_request = github.request
    viewer = ContextVar('panel_demo_viewer', default=None)
    requests = []
    operations = []

    async def delayed_request(method, path, **kwargs):
        operation = viewer.get()
        started = time.perf_counter()
        await asyncio.sleep(max(0, args.github_delay_ms) / 1000)
        result = await fixture_request(method, path, **kwargs)
        if path.endswith('/files'):
            result = [{'filename': f'app/module_{index + 1:02d}.py', 'status': 'modified',
                       'additions': 80, 'deletions': 0,
                       'patch': '@@ -0,0 +1,80 @@\n' + '\n'.join(
                           f'+value_{line} = "sample {line}"' for line in range(80))}
                      for index in range(40)]
        elif '/pulls/' in path:
            result = {**result, 'changed_files': 40, 'additions': 3200, 'deletions': 0}
        if operation is not None:
            requests.append({'operation': operation, 'path': path,
                             'start_ms': round((started - operations[operation]['start']) * 1000),
                             'end_ms': round((time.perf_counter() - operations[operation]['start']) * 1000)})
        return result

    github.request = delayed_request

    @app.middleware('http')
    async def measure(request, call_next):
        if not request.url.path.endswith('/pull-request'):
            return await call_next(request)
        operation = len(operations)
        operations.append({'start': time.perf_counter()})
        token = viewer.set(operation)
        try:
            response = await call_next(request)
            operations[operation]['status'] = response.status_code
            return response
        finally:
            operations[operation]['elapsed_ms'] = round((time.perf_counter() - operations[operation]['start']) * 1000)
            viewer.reset(token)

    @app.get('/demo/panel-performance-login')
    async def login():
        response = RedirectResponse('/demo/panel-workspace#run=' + app.state.demo_run_id)
        app.state.security.new_session(response, identity=IDENTITY)
        return response

    @app.get('/demo/panel-workspace')
    async def workspace():
        html = (args.source_root / 'app/static/index.html').read_text()
        return HTMLResponse(html.replace('<head>', '<head><script src="/demo/panel-probe.js"></script>'))

    @app.get('/demo/panel-probe.js')
    async def probe():
        return Response(PROBE, media_type='text/javascript')

    @app.get('/demo/panel-measurements')
    async def measurements():
        return {'operations': [{key: value for key, value in row.items() if key != 'start'} for row in operations],
                'requests': requests}

    print(f'Local performance demo: http://localhost:{args.port}/demo/panel-performance-login', flush=True)
    print(f'Source checkout: {args.source_root}', flush=True)
    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='panel-loading-') as directory:
        uvicorn.run(build(directory), host='127.0.0.1', port=args.port)
