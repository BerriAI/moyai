"""Record actual local broker requests with disposable skills, notes and identities.

uv run python -m scripts.initial_context_demo --output /tmp/moyai-context-demo --delay 3

Inference is captured by a synthetic upstream; no live model, cloud sandbox or
production data is used. This demonstrates context delivery, not answer quality.
"""
import argparse
import ast
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx

from app.config import Settings
from app.context_budget import ModelContextLimits, counting_input
from app.main import create_app
from app.memory import MAX_CONTEXT
from app.security import digest
from sandbox.tool_guidance import tool_guidance


def prompt_size(source):
    assignment = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'system_message' for target in node.targets))
    prompt = eval(compile(ast.Expression(assignment.value), '<runtime-prompt>', 'eval'),
                  {'__builtins__':{}, 'tool_guidance':tool_guidance, 'harness':'codex', 'spec':{}})
    return len(prompt)


def demonstrate(output, delay, baseline):
    output.mkdir(parents=True, exist_ok=True)
    events, transcript, captured = [], [], []
    started = time.monotonic()

    def log(message):
        print(message, flush=True)
        transcript.append(message)
        events.append([round(time.monotonic()-started, 3), 'o', message+'\r\n'])
        time.sleep(delay)

    with TemporaryDirectory(prefix='moyai-context-demo-') as directory:
        # Explicit defaults prevent the demo from inheriting real credentials.
        values = {key:field.get_default(call_default_factory=True) for key,field in Settings.model_fields.items()}
        values.update(data_dir=Path(directory), public_url='http://127.0.0.1:8787',
            google_client_id='demo-client', google_client_secret='demo-secret',
            google_admin_emails='demo@berri.ai', temporal_enabled=False)
        app = create_app(Settings(_env_file=None, **values))
        store = app.state.store
        with TestClient(app, base_url=values['public_url'], client=('127.0.0.1',50000)) as client:
            response = JSONResponse({})
            sid = app.state.security.new_session(response, identity={'sub':'demo', 'email':'demo@berri.ai',
                'domain':'berri.ai', 'name':'Demo'})
            client.cookies.set('workspace_session', response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0])
            client.headers.update({'Origin':values['public_url'], 'X-CSRF-Token':app.state.security.csrf(sid)})
            assert client.get('/api/session').json()['authenticated']
            memory = client.post('/api/memory', json={'key':'response-style', 'title':'Response style',
                'content':'Keep explanations concise.', 'kind':'preference','request_id':'demo-memory-save'})
            skill = client.post('/api/skills', json={'name':'benchmark-review', 'description':'Review benchmark coverage and results.',
                'instructions':'Count every benchmark case and report failures.', 'scope':'personal','client_id':'demo-skill-save'})
            assert memory.status_code == skill.status_code == 201
            run = store.create_run('Review benchmark coverage.', '', 'modal', [], chat_enabled=True,
                user_id='google:demo', model='openai/gpt-6-astra')
            store.claim_message(run['id'])
            store.update_run(run['id'], status='running', token_hash=digest('demo-capability'))
            run = store.run(run['id'])
            app.state.settings.litellm_api_base = 'https://gateway.example/v1'
            async def limits(model):
                return ModelContextLimits(context_window=200_000, max_input_tokens=190_000, max_output_tokens=10_000)
            async def count(payload, **kwargs):
                return counting_input(payload)[1], 'demo_bytes'
            def upstream(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={'id':'demo', 'output':[], 'usage':{'total_tokens':10}})
            actual = httpx.AsyncClient
            with patch.object(app.state.context_budget, 'limits', limits), patch.object(app.state.context_budget, 'count', count), \
                 patch('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw)):
                log('MOYAI | Real local broker requests; synthetic identities + inference upstream\n'
                    '$ uv run python -m scripts.initial_context_demo --output /tmp/moyai-context-demo')
                headers = {'Authorization':'Bearer demo-capability'}
                url = f"/broker/{run['id']}/v1/responses"
                def inference():
                    reply = client.post(url, headers=headers, json={'input':[], 'stream':False})
                    assert reply.status_code == 200, reply.text
                    return captured[-1]['instructions']
                first = inference()
                assert 'Keep explanations concise.' in first
                assert 'Review benchmark coverage and results.' in first
                assert 'Count every benchmark case' not in first
                assert not store.rows('SELECT * FROM memory_selections') and not store.rows('SELECT * FROM skill_searches')
                log('1. First inference -> HTTP 200\n'
                    '   Memory: Keep explanations concise.\n'
                    '   Skill: personal:benchmark-review — Review benchmark coverage and results.\n'
                    '   Zero memory/skill searches; full procedure absent.')
                loaded = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                    json={'name':'skills_load','arguments':{'name':'personal:benchmark-review'}})
                assert loaded.json()['loaded'] and 'Count every' not in loaded.text
                assert 'Count every benchmark case' in inference()
                log('2. Direct skills_load -> loaded: true\n'
                    '   Next inference contains the full procedure; tool result contains metadata only.')
                # Five valid notes whose JSON array exceeds the cap by exactly
                # one character, while summing note objects alone still fits.
                for i in range(5):
                    saved = client.post('/api/memory', json={'key':f'budget-{i}', 'title':'Budget '+'t'*110,
                        'content':'budget '+'x'*1000, 'kind':'reference','request_id':f'demo-budget-{i}'})
                    assert saved.status_code == 201
                notes = [n for n in app.state.memory.listing('google:demo') if n['key'].startswith('budget-')]
                remaining = MAX_CONTEXT + 1 - len(json.dumps(notes, ensure_ascii=False))
                assert remaining > 0
                for i, note in enumerate(notes):
                    count = min(remaining, 1200-len(note['content']))
                    body = {k:note[k] for k in ('key','title','kind','repo_url','revision')}
                    saved = client.put('/api/memory/'+note['id'], json={**body,
                        'content':note['content']+'x'*count, 'request_id':f'demo-budget-edit-{i}'})
                    assert saved.status_code == 200
                    remaining -= count
                assert remaining == 0
                notes = [n for n in app.state.memory.listing('google:demo') if n['key'].startswith('budget-')]
                candidate_size = len(json.dumps(notes, ensure_ascii=False))
                assert candidate_size == MAX_CONTEXT + 1
                assert sum(len(json.dumps(n, ensure_ascii=False)) for n in notes) <= MAX_CONTEXT
                searched = client.post(f"/broker/{run['id']}/tools/call", headers=headers,
                    json={'name':'memory_search','arguments':{'query':'budget','turn_id':run['active_message_id']}})
                assert searched.status_code == 200
                receipt = searched.json()
                recalled = json.loads(inference().split('PERSONAL MEMORY FOR THE CURRENT REQUESTER.',1)[1].split('\n',1)[1])['notes']
                promised = {n['id'] for n in receipt['matches']}
                delivered = promised & {n['id'] for n in recalled}
                assert receipt['loaded'] == len(promised) == len(delivered) == 4
                assert len(json.dumps(recalled, ensure_ascii=False)) <= MAX_CONTEXT
                log(f'3. Memory search at the {MAX_CONTEXT:,}-character boundary -> HTTP 200\n'
                    f'   Five candidate notes total {candidate_size:,} serialized characters.\n'
                    f'   Search reports {receipt["loaded"]} notes; next inference receives all {len(delivered)}. Brackets and separators count.')
                assert client.post('/api/skills/'+skill.json()['id']+'/archive', json={'archived':True,'revision':1}).status_code == 200
                assert client.put('/api/memory/preferences', json={'enabled':False,'auto_save':False}).status_code == 200
                revoked = inference()
                assert 'Keep explanations concise.' not in revoked and 'Review benchmark coverage' not in revoked
                log('4. Archive skill + pause memory -> next inference removes both\n'
                    '   Requester permissions and settings are checked again.')
                assert 'Keep explanations concise.' not in client.get('/api/runs/'+run['id']).text
                assert 'Count every benchmark case' not in json.dumps(store.events(run['id']))
                assert store.rows('SELECT tainted FROM native_sessions WHERE run_id=?',(run['id'],))[0]['tainted'] & 1
                log('5. Private context stays out of public run data and tool results\n'
                    '   Native-state reuse is disabled for this private turn, as required by existing policy.')
                root = Path(__file__).resolve().parents[1]
                current = prompt_size((root/'sandbox/agent.py').read_text())
                sizes = {'after':current}
                if baseline:
                    before = prompt_size(subprocess.check_output(['git','show',baseline+':sandbox/agent.py'],cwd=root,text=True))
                    sizes['before'] = before
                    log(f'6. Core Codex prompt: {before:,} -> {current:,} characters ({100*(1-current/before):.1f}% smaller)\n'
                        '   Plain web turn, no child/Slack/project additions. Not a token or latency benchmark.')
                else:
                    log(f'6. Core Codex prompt: {current:,} characters\n   Pass --baseline REF to compare with an earlier commit.')
                log('PASS | Context arrives earlier; full skills remain on demand.\n'
                    'This proves broker behavior. It does not measure live-model quality or container startup.')
    header = {'version':2,'width':110,'height':32,'timestamp':int(time.time()),'title':'Moyai initial context — actual local requests'}
    (output/'initial-context.cast').write_text('\n'.join(json.dumps(row) for row in [header,*events])+'\n')
    (output/'verification.txt').write_text('\n\n'.join(transcript)+'\n')
    (output/'results.json').write_text(json.dumps({'prompt_characters':sizes,'model_calls':len(captured),'live_model':False,
        'memory_budget':{'candidate_characters':candidate_size,'limit':MAX_CONTEXT,
                         'search_reported':receipt['loaded'],'delivered':len(delivered)}},indent=2)+'\n')
    page = '''<!doctype html><meta charset="utf-8"><title>Moyai context demo</title>
<style>body{margin:40px;background:#10141d;color:#e8edf5;font:18px/1.55 ui-monospace,monospace}button{font:inherit;padding:6px 16px;margin-right:12px}pre{white-space:pre-wrap}small{color:#9eafc7}</style>
<h2>Moyai · Earlier context, on-demand skills</h2><small>Recorded output of actual local broker requests. Synthetic model upstream.</small>
<p><button id="play">Replay recording</button><button id="end">Show results</button><span id="progress"></span></p><pre id="terminal"></pre>
<script>const events=EVENTS;let timer;const out=document.getElementById('terminal'),progress=document.getElementById('progress');
function play(){clearInterval(timer);out.textContent='';let i=0,start=performance.now();timer=setInterval(()=>{let t=(performance.now()-start)/1000;while(i<events.length&&events[i][0]<=t){out.textContent+=events[i++][2]+'\\n';}progress.textContent=t.toFixed(1)+'s';if(i===events.length)clearInterval(timer);},40)}
document.getElementById('play').onclick=play;document.getElementById('end').onclick=()=>{clearInterval(timer);out.textContent=events.map(e=>e[2]).join('\\n');progress.textContent='Complete'};play();</script>'''
    (output/'recording.html').write_text(page.replace('EVENTS',json.dumps(events).replace('<','\\u003c')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--delay', type=float, default=0)
    parser.add_argument('--baseline', default='')
    args = parser.parse_args()
    if not 0 <= args.delay <= 5:
        parser.error('Use a delay between 0 and 5 seconds.')
    demonstrate(args.output, args.delay, args.baseline)
