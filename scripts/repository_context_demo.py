"""Local context-service demo with live read-only GitHub data via gh.

python -m scripts.repository_context_demo --repo BerriAI/moyai --port 8845
Use --once for JSON measurements. No model or sandbox calls; isolated temp DB.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import statistics
import tempfile
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
import uvicorn

from app.config import Settings
from app.connector_errors import ConnectorError
from app.db import database
from app.main import create_app
from app.security import digest


async def github_get(path):
    process = await asyncio.create_subprocess_exec('gh', 'api', '--method', 'GET', path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('Read-only GitHub request failed. Check gh auth status and repository access.')
    return json.loads(stdout)


async def demonstrate(repository):
    metadata = await github_get('repos/' + repository)
    with tempfile.TemporaryDirectory(prefix='moyai-repository-context-') as directory:
        settings = Settings(_env_file=None, data_dir=Path(directory), public_url='http://127.0.0.1:8845',
            litellm_api_key='', litellm_api_base='', modal_token_id='', modal_token_secret='',
            repository_context_enabled=True, memory_review_enabled=False, session_titles_enabled=False,
            moyai_database_url='', moyai_runtime_role='standalone', moyai_separate_broker=False)
        app = create_app(settings)
        store, github, index = app.state.store, app.state.connectors.github, app.state.repository_context
        requests = []
        target = metadata['id']
        credentials = {'kind': 'github_app', 'installation_id': 1,
            'account_id': metadata['owner']['id'], 'repository_ids': [target]}
        github.save_app({'id': 1, 'owner_id': metadata['owner']['id'], 'pem': 'local-demo-placeholder'})
        app.state.connectors.save('github', credentials, 'Isolated local demo')
        github.remember_repository(metadata, credentials)
        async def token(*args, **kwargs):
            return 'local-gh-transport'
        async def request(method, path, **kwargs):
            if method != 'GET' or not path.startswith(f'/repositories/{target}'):
                raise RuntimeError('The demo only reads its selected repository.')
            from urllib.parse import urlencode
            requests.append(path)
            return await github_get(path.lstrip('/') + ('?' + urlencode(kwargs['params']) if kwargs.get('params') else ''))
        github.installation_token, github.request = token, request
        try:
            run = store.create_run('Understand repository context and its tests', 'https://github.com/' + repository,
                'modal', ['github'], chat_enabled=True)
            message_id = store.messages(run['id'])[0]['id']
            store.update_run(run['id'], status='running', token_hash=digest('local-demo'))
            store.execute('UPDATE runs SET github_repository_id=?,active_message_id=?,active_user_id=? WHERE id=?',
                (target, message_id, 'local-demo', run['id']))
            run = store.run(run['id'])
            start = time.perf_counter()
            cold = await database(index.context, run)
            cold_ms = (time.perf_counter() - start) * 1000
            assert not cold and not requests
            start = time.perf_counter()
            await index.refresh(github.connection_version(), target)
            index_ms = (time.perf_counter() - start) * 1000
            before = len(requests)
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                guide = await database(index.context, run)
                samples.append((time.perf_counter() - start) * 1000)
            warm_requests = len(requests) - before
            assert guide and warm_requests == 0
            start = time.perf_counter()
            found = await index.call(run, 'repository_context_search', {'query': 'context budget'})
            search_ms = (time.perf_counter() - start) * 1000
            chosen = next((item for item in found['matches'] if item['path'].endswith('.py')), found['matches'][0])
            start = time.perf_counter()
            source = await index.call(run, 'repository_context_read', {
                'commit_sha': found['commit_sha'], 'path': chosen['path'], 'max_lines': 12})
            read_ms = (time.perf_counter() - start) * 1000
            store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('github',0) ON CONFLICT(provider) DO UPDATE SET enabled=0")
            assert await database(index.context, run) == ''
            denied = False
            try:
                await index.call(run, 'repository_context_search', {'query': 'context budget'})
            except ConnectorError:
                denied = True
            assert denied
            return {'repository': repository, 'commit_sha': found['commit_sha'], 'source': 'Live GitHub reads via local gh login',
                'cold_guide_ms': round(cold_ms, 2), 'background_index_ms': round(index_ms, 2),
                'warm_guide_p50_ms': round(statistics.median(samples), 2),
                'warm_guide_p95_ms': round(sorted(samples)[18], 2), 'samples': len(samples),
                'guide_bytes': len(guide.encode()), 'github_requests_during_warm_reads': warm_requests,
                'model_calls': 0, 'sandbox_starts': 0, 'search_ms': round(search_ms, 2),
                'source_read_ms': round(read_ms, 2), 'access_revocation_passed': denied,
                'coverage': found['coverage'], 'matches': found['matches'][:6], 'excerpt': source,
                'guide': json.loads(guide.split('\n', 1)[1]),
                'limitation': 'Local context-service measurements. No model inference or completed-task performance comparison.'}
        finally:
            store.close()


HTML = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Moyai · Repository context</title><style>
body{background:#f6f7fb;color:#172033;font:16px system-ui;margin:0}main{max-width:1080px;margin:48px auto;padding:0 32px}
h1{font-size:36px;margin:8px 0}p{color:#546179;line-height:1.6}small{color:#617089}button{background:#5944cb;color:white;border:0;border-radius:8px;padding:13px 22px;font-size:16px;cursor:pointer}button:disabled{opacity:.6}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:25px 0}.card,section{background:white;border:1px solid #e1e5ee;border-radius:12px;padding:20px}.value{font-size:28px;font-weight:650;margin:8px 0}.label{font-size:13px;color:#56657d}section{margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f9;padding:15px;font-size:13px}a{color:#5944cb}.ok{color:#147446;font-weight:600}li{margin:7px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}.status{margin:18px 0;font-weight:500}
</style></head><body><main><small>MOYAI / SHARED REPOSITORY CONTEXT</small><h1>Useful context before the computer starts</h1>
<p>This local demo builds a guide from live GitHub data, measures cached reads, retrieves a source excerpt, and checks access revocation. It runs Moyai’s actual context service in an isolated database.</p>
<button id="run">Run live comparison</button><div id="status" class="status">Ready. No model calls or sandbox required.</div><div id="result"></div>
<p><small>These are local service timings. Model response time and completed-task quality are not measured.</small></p></main>
<script>
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
document.querySelector('#run').onclick=async()=>{const button=document.querySelector('#run');button.disabled=true;document.querySelector('#result').innerHTML='';document.querySelector('#status').textContent='Reading GitHub and preparing the guide outside the task startup path…';try{
const response=await fetch('/probe',{method:'POST',headers:{'X-Local-Demo':'1'}});const d=await response.json();if(!response.ok)throw Error(d.detail||'Demo failed');
document.querySelector('#status').innerHTML='<span class="ok">Verified: cached reads made zero GitHub calls; disabling access blocked cached context.</span>';
const card=(value,label)=>`<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`;
document.querySelector('#result').innerHTML=`<div class="grid">${card(d.warm_guide_p50_ms+' ms','Cached guide · median of 20')}${card(d.guide_bytes.toLocaleString()+' B','Initial guide size')}${card(d.github_requests_during_warm_reads,'GitHub calls during cached reads')}${card(d.model_calls+' / '+d.sandbox_starts,'Model calls / sandbox starts')}</div>
<section><b>${esc(d.repository)}</b> · <small>Snapshot ${esc(d.commit_sha.slice(0,12))}</small><p>Indexed ${d.coverage.indexed_paths} paths and ${d.coverage.indexed_documents} orientation documents. Background preparation: ${(d.background_index_ms/1000).toFixed(2)} s. Cold guide lookup: ${d.cold_guide_ms} ms. Cached guide p95: ${d.warm_guide_p95_ms} ms.</p></section>
<div class="two"><section><b>Task: context budget</b><p>Cached discovery: ${d.search_ms} ms</p><ul>${d.matches.map(m=>`<li><a href="${esc(m.url)}" target="_blank" rel="noreferrer">${esc(m.path)}</a></li>`).join('')}</ul></section><section><b>Source loaded on demand</b><p>${esc(d.excerpt.path)} · ${d.source_read_ms} ms</p><pre>${esc(d.excerpt.content)}</pre></section></div>
<details><summary>Inspect the actual result</summary><pre>${esc(JSON.stringify(d,null,2))}</pre></details>`;
}catch(error){document.querySelector('#status').textContent=error.message}finally{button.disabled=false}};
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='BerriAI/moyai')
    parser.add_argument('--port', type=int, default=8845)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('Use owner/repository.')
    if args.once:
        print(json.dumps(asyncio.run(demonstrate(args.repo)), indent=2))
        return
    demo = FastAPI()
    lock = asyncio.Lock()
    @demo.get('/', response_class=HTMLResponse)
    async def home():
        return HTML
    @demo.post('/probe')
    async def probe(request: Request):
        if request.headers.get('X-Local-Demo') != '1' or request.headers.get('host') != f'127.0.0.1:{args.port}':
            raise HTTPException(403, 'Open the local demo page.')
        if lock.locked():
            raise HTTPException(409, 'A demonstration is already running.')
        async with lock:
            try:
                return await demonstrate(args.repo)
            except Exception as exc:
                raise HTTPException(500, f'Demo did not complete ({type(exc).__name__}). Check local gh access.') from None
    uvicorn.run(demo, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
