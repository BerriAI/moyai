"""Opt-in live steering comparison: native Codex HTTP Responses vs the full app.

Sends synthetic writing prompts to the selected gateway and incurs model usage.
Requires the repository test helpers, local Temporal, and optionally the Codex CLI.
It measures receipt, first redirected text and completion separately. It is not a
benchmark of OpenAI's direct WebSocket response.steer or real external tool writes.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from cryptography.fernet import Fernet
from app.config import Settings
from app.main import create_app
from test_deepagents_acceptance import web_worker, create, until

OUT = None
MODEL = BASE = KEY = None
TRIALS = 3
PROMPT = ('Draft a detailed 25-section plan for a community picnic named Willow. '
          'Include timing, supplies, food, games, volunteer assignments, and contingencies. '
          'This is a writing exercise: do not use tools, browse, read files, or ask questions.')
CORRECTION = ('Change Willow to an indoor board game evening for 12 people. '
              'Stop expanding the picnic plan. Keep the name Willow. '
              'Finish with at most 6 short bullets, explicitly mention 12 people and board games, '
              'and use INDOOR_PLAN as the first word of your revised answer. Do not use tools.')

async def native(trial):
    env = dict(os.environ, LITELLM_DEV_API_KEY=KEY)
    cwd = OUT / 'native-workspace'
    cwd.mkdir(exist_ok=True)
    args = [shutil.which('codex'), 'app-server', '--listen', 'stdio://',
            '-c', 'model_provider="steering_probe"',
            '-c', 'model_providers.steering_probe.name="Steering probe"',
            '-c', f'model_providers.steering_probe.base_url="{BASE}"',
            '-c', 'model_providers.steering_probe.env_key="LITELLM_DEV_API_KEY"',
            '-c', 'model_providers.steering_probe.wire_api="responses"',
            '-c', 'model_providers.steering_probe.requires_openai_auth=false',
            '-c', 'model_providers.steering_probe.supports_websockets=false']
    import tomllib
    config=Path.home()/'.codex/config.toml'
    if config.exists():
        for name in tomllib.loads(config.read_text()).get('mcp_servers',{}):
            args.extend(['-c', f'mcp_servers.{name}.enabled=false'])
    log = (OUT / f'native-steering-{trial}.log').open('wb')
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=log, cwd=cwd, env=env)
    async def send(method, params=None, identity=None):
        value={'method':method}
        if params is not None: value['params']=params
        if identity is not None: value['id']=identity
        proc.stdin.write((json.dumps(value)+'\n').encode())
        await proc.stdin.drain()
    async def receive():
        line=await proc.stdout.readline()
        if not line: raise RuntimeError('Native Codex exited')
        return json.loads(line)
    async def rpc(method, params, identity):
        await send(method, params, identity)
        while True:
            value=await receive()
            if value.get('id')==identity:
                if 'error' in value: raise RuntimeError(str(value['error']))
                return value['result']
    try:
        async with asyncio.timeout(180):
            await rpc('initialize',{'clientInfo':{'name':'moyai-steering-qualification','title':'Moyai qualification','version':'1'}},1)
            await send('initialized')
            thread=(await rpc('thread/start',{'model':MODEL,'cwd':str(cwd),'approvalPolicy':'never','sandbox':'read-only','ephemeral':True},2))['thread']['id']
            started=time.monotonic()
            turn=(await rpc('turn/start',{'threadId':thread,'input':[{'type':'text','text':PROMPT}],'effort':'medium'},3))['turn']['id']
            steered=None;ack=None;first=None;text=[];items=[]
            while True:
                value=await receive()
                method, params=value.get('method'),value.get('params',{})
                if method=='item/agentMessage/delta':
                    delta=params.get('delta',''); text.append(delta)
                    if steered is None:
                        steered=time.monotonic()
                        await send('turn/steer',{'threadId':thread,'expectedTurnId':turn,'input':[{'type':'text','text':CORRECTION}]},4)
                    if 'INDOOR_PLAN' in ''.join(text) and first is None: first=time.monotonic()
                if value.get('id')==4:
                    if 'error' in value: raise RuntimeError(str(value['error']))
                    if value['result']['turnId'] != turn:
                        raise RuntimeError('Native steering changed turns')
                    ack=time.monotonic()
                if method=='item/completed' and params.get('item',{}).get('type')=='agentMessage':
                    items.append(params['item'].get('text',''))
                if method=='turn/completed':
                    if params['turn']['id'] != turn:
                        raise RuntimeError('Native completion changed turns')
                    status=params['turn']['status']
                    if status!='completed': raise RuntimeError('Native status: '+status)
                    break
            final=items[-1] if items else ''.join(text)
            return {'engine':'native_codex','trial':trial,'ack_ms':round((ack-steered)*1000,1) if ack and steered else None,
                'redirected_text_ms':round((first-steered)*1000,1) if first and steered else None,
                'completion_ms':round((time.monotonic()-steered)*1000,1) if steered else None,
                'same_turn':True,'adopted':all(s.lower() in final.lower() for s in ['Willow','12','board','INDOOR_PLAN']), 'final':final}
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()
        log.close()

async def deepagents():
    with tempfile.TemporaryDirectory(prefix='live-deepagents-',dir=OUT) as directory:
        settings=Settings(_env_file=None,data_dir=Path(directory),temporal_enabled=True,
            session_secret='synthetic-local-live-probe',encryption_key=Fernet.generate_key().decode(),
            agent_model=MODEL,session_titles_enabled=False,memory_review_enabled=False,skill_learning_enabled=False)
        app=create_app(settings)
        manager,store=app.state.manager,app.state.store
        request=manager.deepagents_runtime.model_request
        async def medium(scope,messages,options,parameters):
            return await request(scope,messages,{**options,'reasoning':{'effort':'medium'}},parameters)
        manager.deepagents_runtime.model_request=medium
        try:
            async with web_worker(app) as api:
                settings.litellm_api_base=BASE
                settings.litellm_api_key=KEY
                for trial in range(1,TRIALS+1):
                    run_id=await create(api,PROMPT)
                    await until(lambda:any(e['data'].get('phase')=='streaming' for e in store.events(run_id,limit=1000)) or store.run(run_id)['status']=='failed',seconds=120)
                    if store.run(run_id)['status']=='failed':
                        raise RuntimeError('Deep Agents failed before steering: '+store.run(run_id)['summary'])
                    turn=store.run(run_id)['active_message_id']
                    started=time.monotonic()
                    response=await api.post(f'/api/runs/{run_id}/messages',json={'content':CORRECTION,'client_id':f'live-steering-{trial}','send_now':True})
                    if response.status_code!=202: raise RuntimeError('Steer rejected: '+str(response.status_code)+' '+response.text)
                    received=time.monotonic(); correction=response.json()['id']
                    await until(lambda:store.rows('SELECT status FROM messages WHERE id=?',(correction,))[0]['status']=='injected',seconds=20)
                    accepted=time.monotonic(); first=None
                    async with asyncio.timeout(150):
                        while store.run(run_id)['status'] not in {'idle','failed','cancelled'}:
                            if first is None and any('INDOOR_PLAN' in e['message'] and e['data'].get('input_id')==correction for e in store.events(run_id,limit=1000)):
                                first=time.monotonic()
                            await asyncio.sleep(.02)
                    final=store.run(run_id)['summary']
                    value={'engine':'deepagents_temporal','trial':trial,'http_receipt_ms':round((received-started)*1000,1),
                        'ack_ms':round((accepted-started)*1000,1),'redirected_text_ms':round((first-started)*1000,1) if first else None,
                        'completion_ms':round((time.monotonic()-started)*1000,1),'same_turn':store.run(run_id)['active_message_id']==turn,
                        'adopted':all(s.lower() in final.lower() for s in ['Willow','12','board','INDOOR_PLAN']),
                        'status':store.run(run_id)['status'],'final':final}
                    results.append(value);save();print(json.dumps({k:v for k,v in value.items() if k!='final'}),flush=True)
        finally: store.close()

results=[]
def save(): (OUT/'live-steering-comparison.json').write_text(json.dumps({'model':MODEL,'gateway':BASE,'reasoning':'medium','results':results},indent=2))
async def main(engine):
    if engine in {'both', 'native'}:
        for trial in range(1, TRIALS + 1):
            try:
                value = await native(trial)
            except Exception as exc:
                value = {'engine': 'native_codex', 'trial': trial, 'error': type(exc).__name__}
            results.append(value)
            save()
            print(json.dumps({k: v for k, v in value.items() if k != 'final'}), flush=True)
    if engine in {'both', 'deepagents'}:
        await deepagents()
    if not results or any(not r.get('adopted') or not r.get('same_turn') for r in results):
        raise SystemExit('One or more steering trials failed; inspect the output report.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh local output directory')
    parser.add_argument('--gateway', default='https://gateway-dev.litellm-sandbox.ai/v1')
    parser.add_argument('--model', default='openai/gpt-6-astra')
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--engine', choices=['both', 'native', 'deepagents'], default='both')
    parser.add_argument('--env-file', type=Path, help='Optional dotenv file containing LITELLM_DEV_API_KEY')
    args = parser.parse_args()
    if not 1 <= args.trials <= 20:
        parser.error('--trials must be between 1 and 20')
    KEY = os.environ.get('LITELLM_DEV_API_KEY', '')
    if not KEY and args.env_file:
        from dotenv import dotenv_values
        KEY = dotenv_values(args.env_file).get('LITELLM_DEV_API_KEY', '')
    if not KEY:
        parser.error('Set LITELLM_DEV_API_KEY or supply --env-file; the production key is not used')
    if args.engine in {'both', 'native'} and not shutil.which('codex'):
        parser.error('Native comparison requires the Codex CLI in PATH')
    OUT, MODEL, BASE, TRIALS = args.output.resolve(), args.model, args.gateway.rstrip('/'), args.trials
    OUT.mkdir(parents=True, exist_ok=False)
    asyncio.run(main(args.engine))
