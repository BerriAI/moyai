"""Run: uv run python scripts/slack_search_demo.py; open http://127.0.0.1:8797/demo.

Real broker and Slack connector, with a simulated Slack HTTP API and safe data.
"""
import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi.responses import HTMLResponse, Response

from app.config import Settings
from app.connectors import Search
from app.main import create_app
from app.security import digest


PAGE = '''<!doctype html><meta charset="utf-8"><title>Moyai · Slack channel search</title>
<style>
body{font:18px system-ui;background:#f7f6fb;color:#29243c;margin:0;padding:36px}main{max-width:1100px;margin:auto}h1{font-size:38px;margin:12px 0}p{color:#625d72;line-height:1.5}button{background:#5b3fd1;color:white;border:0;border-radius:8px;padding:12px 18px;font:inherit;cursor:pointer}button:disabled{opacity:.5}.badge{color:#5b3fd1;font-size:14px;font-weight:700;letter-spacing:.06em}.card{background:white;border:1px solid #ddd7ed;border-radius:12px;padding:22px;margin-top:20px}label{display:block;font-weight:650;margin-bottom:10px}input{box-sizing:border-box;width:100%;font:16px monospace;padding:14px;border:1px solid #afa4cd;border-radius:8px;margin-bottom:16px}pre{font:16px monospace;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6;margin:8px 0 20px}.ok{color:#245d42;font-weight:650}small{color:#625d72}#status{margin-left:15px}
</style><main><div class="badge">MOYAI · LOCAL BROKER VERIFICATION</div>
<h1>Search the intended Slack channel.</h1>
<p>A bare channel ID becomes Slack's canonical channel filter.<br>Real broker and connector requests. Slack's API and returned message are simulated.</p>
<section class="card"><label for="query">Search query</label><input id="query" value="in:C0123456789 before:2026-10-11 after:2026-10-08">
<button id="run">Run search</button><span id="status">Ready</span></section>
<section class="card" id="result" hidden><label>Actual outbound query</label><pre id="outbound"></pre><label>Broker response · simulated Slack result</label><pre id="reply"></pre><div class="ok" id="checks"></div></section>
<p><small>Preserves date filters, quoted text and channel names. One search request; no channel lookup.</small></p></main>
<script>
document.querySelector('#run').onclick=async()=>{const button=document.querySelector('#run');button.disabled=true;document.querySelector('#status').textContent='Searching…';
try{const response=await fetch('/demo/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.querySelector('#query').value})});if(!response.ok)throw Error('HTTP '+response.status);const data=await response.json();document.querySelector('#outbound').textContent=data.query;document.querySelector('#reply').textContent=JSON.stringify(data.response);document.querySelector('#checks').textContent=data.requests+' upstream request · Channel filter retained';document.querySelector('#result').hidden=false;document.querySelector('#status').textContent='Search completed';}catch(error){document.querySelector('#status').textContent='Failed: '+error.message;}finally{button.disabled=false}};
</script>'''


def demo(directory):
    values = {key: field.get_default(call_default_factory=True) for key, field in Settings.model_fields.items()}
    values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8797', auto_prepare_repositories=False)
    app = create_app(Settings(_env_file=None, **values))
    lock = asyncio.Lock()
    original_client = httpx.AsyncClient

    @app.get('/demo', response_class=HTMLResponse)
    async def page():
        return PAGE.split('<script>')[0] + '<script src="/demo/script.js"></script>'

    @app.get('/demo/script.js')
    async def script():
        return Response(PAGE.split('<script>')[1].split('</script>')[0], media_type='text/javascript')

    @app.post('/demo/search')
    async def search(body: Search):
        async with lock:
            store = app.state.store
            app.state.connectors.save('slack', {'access_token': 'local-demo-placeholder', 'kind': 'personal'}, 'Simulated Slack')
            run = store.create_run('Local channel search verification', '', 'modal', ['slack'])
            store.update_run(run['id'], status='running', token_hash=digest('local-demo-capability'))
            requests = []

            def slack_api(request):
                assert request.url.host == 'slack.com' and request.url.path == '/api/search.messages'
                requests.append(request.url.params['query'])
                # Fixture behavior makes the syntax regression visible without live credentials.
                matches = [{'text': 'Sample fallback feedback', 'channel': {'id': 'C0123456789'}}] if 'in:<#C0123456789>' in requests[-1] else []
                return httpx.Response(200, json={'ok': True, 'messages': {'matches': matches}})

            def slack_client(**kwargs):
                return original_client(transport=httpx.MockTransport(slack_api), **kwargs)

            try:
                async with original_client(transport=httpx.ASGITransport(app=app), base_url=values['public_url']) as client:
                    with patch('app.connectors.httpx.AsyncClient', slack_client):
                        response = await client.post(f"/broker/{run['id']}/tools/call",
                            headers={'Authorization': 'Bearer local-demo-capability'},
                            json={'name': 'slack_search', 'arguments': body.model_dump()})
                response.raise_for_status()
                assert len(requests) == 1
                return {'query': requests[0], 'response': response.json(), 'requests': len(requests)}
            finally:
                store.update_run(run['id'], status='completed', token_hash='')

    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='moyai-slack-search-demo-') as directory:
        uvicorn.run(demo(directory), host='127.0.0.1', port=8797)
