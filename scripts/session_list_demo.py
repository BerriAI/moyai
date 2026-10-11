"""Measure real session-list APIs with synthetic SQLite data, or open the UI.

uv run python scripts/session_list_demo.py --port 8846
uv run python scripts/session_list_demo.py --benchmark --output /tmp/list.json
Use --root /path/to/baseline for the same fixture on an older checkout.
No external models, authentication providers or connected services are called.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
import time


PROBE = r"""
const originalFetch=window.fetch;
let sample=null;
window.fetch=async(...args)=>{
  const started=performance.now(),response=await originalFetch(...args);
  if(String(args[0]).startsWith('/api/runs?')){
    const text=await response.clone().text();
    sample={ms:Math.round(performance.now()-started),bytes:new TextEncoder().encode(text).length,
      queries:response.headers.get('x-demo-queries'),sessions:JSON.parse(text).length};paint();
  }
  return response;
};
function paint(){
  const output=document.querySelector('#session-list-metrics');if(!output)return;
  output.textContent=sample?`${sample.sessions} sessions loaded\nAPI: ${sample.ms} ms\nResponse: ${(sample.bytes/1024).toFixed(1)} KiB\nSQL statements: ${sample.queries}`:'Loading sessions…';
}
window.addEventListener('DOMContentLoaded',()=>{
  const panel=document.createElement('aside');panel.style='position:fixed;right:24px;bottom:24px;background:white;border:1px solid #7354ac;border-radius:12px;padding:20px;width:300px;max-width:calc(100vw - 48px);box-sizing:border-box;z-index:9999;font:14px/1.5 system-ui';
  panel.innerHTML='<b>Session loading · local verification</b><p>100 sessions + 60 agents.<br>32 KiB saved answers per session.<br>Real SQLite API, synthetic data.<br>No injected latency.</p><pre id="session-list-metrics" style="font:14px/1.8 monospace"></pre><button id="reload-list-demo" style="padding:8px 12px">Reload workspace</button>';
  document.body.append(panel);document.querySelector('#reload-list-demo').onclick=()=>location.reload();paint();
});
"""


def build(root, directory, port):
    from fastapi.responses import HTMLResponse, RedirectResponse, Response
    from scripts.session_ui_demo import demo

    app = demo(directory, port)
    store = app.state.store
    owner = store.identity({'method': 'google', 'identity': {
        'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex'}})
    summary = 'Saved synthetic answer. ' * 1400
    with store.connect() as conn:
        for index in range(100):
            identity = f'{index + 1:032x}'
            stamp = f'2026-10-07T12:{index // 60:02d}:{index % 60:02d}+00:00'
            conn.execute('''INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,
                created_at,updated_at,owner_id,chat_enabled,display_title,summary)
                VALUES(?,?,'','demo','idle','[]',?,?,?,1,?,?)''',
                (identity, f'Investigate session {index + 1}', stamp, stamp, owner,
                 f'Investigate session {index + 1}', summary))
            if index >= 80:
                for child in range(3):
                    conn.execute('''INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,
                        created_at,updated_at,owner_id,parent_run_id,agent_label,summary)
                        VALUES(?,?,'','demo','idle','[]',?,?,?,?,?,?)''',
                        (f'{1000 + index * 3 + child:032x}', 'Agent analysis ' + summary,
                         stamp, stamp, owner, identity, f'Worker {child + 1}', summary))
        # Keep the fixture from session_ui_demo outside the latest 100 rows.
        conn.execute("UPDATE runs SET updated_at='2020-01-01' WHERE owner_id!=?", (owner,))

    counts = ContextVar('session_list_queries', default=None)
    connect = store.connect

    @contextmanager
    def instrumented_connect():
        with connect() as conn:
            count = counts.get()
            if count is not None:
                conn.set_trace_callback(lambda _sql: count.append(1))
            yield conn

    store.connect = instrumented_connect

    @app.middleware('http')
    async def measure(request, call_next):
        queries = []
        token = counts.set(queries)
        try:
            response = await call_next(request)
            response.headers['x-demo-queries'] = str(len(queries))
            return response
        finally:
            counts.reset(token)

    @app.get('/demo/list-login')
    async def login():
        response = RedirectResponse('/demo/session-list#tasks')
        app.state.security.new_session(response, identity={
            'sub': 'demo-alex', 'email': 'alex@example.com', 'name': 'Alex', 'domain': 'example.com'})
        return response

    @app.get('/demo/session-list')
    async def workspace():
        return HTMLResponse((root / 'app/static/index.html').read_text().replace(
            '<head>', '<head><script src="/demo/session-list-probe.js"></script>'))

    @app.get('/demo/session-list-probe.js')
    async def probe():
        return Response(PROBE, media_type='text/javascript')

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--port', type=int, default=8846)
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    with TemporaryDirectory(prefix='session-list-') as directory:
        app = build(root, directory, args.port)
        if args.benchmark:
            from fastapi.testclient import TestClient
            client = TestClient(app, base_url=f'http://127.0.0.1:{args.port}')
            client.get('/demo/list-login')
            samples = []
            for _ in range(7):
                start = time.perf_counter()
                response = client.get('/api/runs?scope=all&view=sidebar')
                response.raise_for_status()
                samples.append((time.perf_counter() - start) * 1000)
            result = {'source': str(root), 'sessions': len(response.json()),
                      'median_ms': round(statistics.median(samples), 2),
                      'samples_ms': [round(value, 2) for value in samples],
                      'response_bytes': len(response.content),
                      'sql_statements': int(response.headers['x-demo-queries'])}
            text = json.dumps(result, indent=2)
            print(text)
            if args.output:
                args.output.write_text(text + '\n')
            client.close()
        else:
            import uvicorn
            print(f'Local demo: http://127.0.0.1:{args.port}/demo/list-login', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
