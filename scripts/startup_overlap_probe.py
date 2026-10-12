"""Measure the actual durable controller with controlled source/provider I/O.

No cloud, broker, model, or connected-app requests. --source can select a baseline
checkout without modifying it. --serve presents live subprocess output locally.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace


async def sample(args, number):
    # Select before importing any app modules, including in baseline subprocesses.
    sys.path.insert(0, str(args.source.resolve()))
    import modal
    from app.config import Settings
    from app.db import Store
    from app.durable_runner import DurableRunner
    import app.durable_runner as runtime

    events, machines, launches = [], {}, []
    began = time.monotonic()
    context_ready = False

    def event(name):
        item = {'sample': number, 'event': name, 'ms': round((time.monotonic() - began) * 1000, 2)}
        events.append(item)
        if not args.quiet:
            print(json.dumps(item), flush=True)

    class Machine:
        object_id = 'sb-startup-probe'

        def __init__(self):
            self.filesystem = SimpleNamespace(write_text=SimpleNamespace(aio=self.write))
            self.poll = SimpleNamespace(aio=self.poll_impl)

        async def write(self, data, path):
            self.spec = json.loads(data)

        async def poll_impl(self):
            return None

    class Provider:
        async def find(self, name, **kwargs):
            if name not in machines:
                raise modal.exception.NotFoundError('Fixture machine absent')
            return machines[name]

        async def create(self, *, name, **kwargs):
            event('provider_started')
            await asyncio.sleep(args.provider_delay)
            machines[name] = Machine()
            event('provider_ready')
            return machines[name]

        async def get(self, identity):
            return next(machine for machine in machines.values() if machine.object_id == identity)

    async def source(identity):
        nonlocal context_ready
        event('context_started')
        await asyncio.sleep(args.context_delay)
        context_ready = True
        event('context_ready')

    async def upload(machine):
        event('runtime_upload_fixture')

    async def command(machine, action, directory, value, *, token=None):
        assert action == 'start' and context_ready and len(machines) == 1
        assert machine.spec['prompt'] == 'Summarize the prepared source'
        launches.append(directory)
        event('agent_launch_boundary')
        return ''

    with TemporaryDirectory(prefix='moyai-startup-probe-') as directory:
        settings = Settings(_env_file=None, data_dir=Path(directory),
            session_secret='local-fixture-key', agent_model='test-model',
            sandbox_idle_seconds=0, sandbox_prepared_pool_size=0, max_concurrent_runs=4)
        store = Store(Path(directory))
        manager = DurableRunner(store, settings)
        manager.provider = lambda *a, **kw: Provider()
        manager.prepare_context, manager.command = source, command
        runtime.refresh_sandbox_files = upload
        began = time.monotonic()
        event('submission')
        run = store.create_run('Summarize the prepared source', '', 'modal', [],
                               chat_enabled=True, harness='codex', model='test-model')
        manager.submit(run)
        try:
            async with asyncio.timeout(args.context_delay + args.provider_delay + 15):
                while not launches:
                    await manager.advance(run['id'])
            assert len(launches) == 1 and manager.state(run['id'])['phase'] == 'monitor'
        finally:
            store.close()
    return {'submission_to_launch_ms': events[-1]['ms'], 'events': events,
            'machines': len(machines), 'launches': len(launches)}


async def measure(args):
    samples = [await sample(args, i + 1) for i in range(args.samples)]
    times = sorted(item['submission_to_launch_ms'] for item in samples)
    result = {'event': 'summary', 'kind': 'controlled_fixture', 'source': str(args.source.resolve()),
        'metric': 'Persisted submission to agent launch boundary; no inference executed',
        'harness': 'codex', 'model': 'test-model (not invoked)', 'cold_samples': len(samples),
        'context_delay_seconds': args.context_delay, 'provider_delay_seconds': args.provider_delay,
        'median_ms': round(statistics.median(times), 2),
        'p95_ms': times[math.ceil(len(times) * .95) - 1],
        'min_ms': min(times), 'max_ms': max(times), 'samples': samples}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Moyai startup comparison</title><style>
body{margin:0;background:#f5f4fa;color:#211c35;font:16px/1.6 system-ui}
main{max-width:1040px;margin:40px auto;padding:0 24px}h1{font-size:34px;margin:8px 0}
.label{color:#6246b9;font-weight:700;letter-spacing:.12em;font-size:12px}
p{max-width:900px}button{background:#5b3fd1;color:white;border:0;border-radius:8px;padding:12px 22px;font:inherit;cursor:pointer}
button:disabled{opacity:.55}section{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:26px}
article{background:white;border:1px solid #ddd7ee;border-radius:12px;padding:24px}h2{margin:0;font-size:21px}
.time{font-size:40px;font-weight:700;margin:10px 0}.events{font:14px/1.8 ui-monospace,monospace;white-space:pre-wrap}
small{color:#635b71}.notice{padding:16px;background:#ebe7f8;border-radius:8px}
@media(max-width:700px){section{grid-template-columns:1fr}}
</style><main><div class="label">MOYAI · FIRST STARTUP PR</div>
<h1>Load context while the workspace starts</h1>
<p>Live diagnostic of the real durable controller. Source loading waits 3 seconds;
the simulated provider waits 4 seconds. Both must finish before the agent launch boundary.</p>
<p class="notice">Controlled local fixture. No model inference, cloud machine, Slack request,
or user task is executed. This measures startup scheduling, not production reply latency.</p>
<button id="run">Run comparison</button><span id="status" role="status"></span>
<section><article><h2>Baseline · sequential</h2><div class="time" id="baseline-time">—</div>
<small>Submission → agent launch boundary</small><div class="events" id="baseline"></div></article>
<article><h2>Candidate · overlapped</h2><div class="time" id="candidate-time">—</div>
<small>Submission → agent launch boundary</small><div class="events" id="candidate"></div></article></section>
<p>One durable acquisition owner. Complete context before launch. Generated commands still run inside the sandbox.</p>
<script>
const button=document.querySelector('#run');
button.onclick=async()=>{
 button.disabled=true;document.querySelector('#status').textContent=' Running…';
 for(const name of ['baseline','candidate']){document.getElementById(name).textContent='';document.getElementById(name+'-time').textContent='—';}
 try {
  const response=await fetch('/run',{method:'POST'});if(!response.ok)throw Error('Demo unavailable');
  const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='';
  while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
   const lines=buffer.split('\\n');buffer=lines.pop();
   for(const line of lines){if(!line)continue;const item=JSON.parse(line);
    if(item.error)throw Error(item.error);
    if(item.event==='summary')document.getElementById(item.label+'-time').textContent=(item.median_ms/1000).toFixed(2)+' s';
    else document.getElementById(item.label).textContent+=(item.ms/1000).toFixed(2)+' s  '+item.event.replaceAll('_',' ')+'\\n';
   }
  }document.querySelector('#status').textContent=' Comparison complete';
 }catch(error){document.querySelector('#status').textContent=error.message;}finally{button.disabled=false;}
};</script></main></html>'''


def serve(args):
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, StreamingResponse
    app = FastAPI()
    gate = asyncio.Lock()

    @app.get('/')
    async def page():
        return HTMLResponse(PAGE)

    @app.post('/run')
    async def run():
        async def output():
            async with gate:
                for label, source in [('baseline', args.baseline), ('candidate', args.source)]:
                    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                        '--source', str(source.resolve()), '--samples', '1', '--context-delay', '3', '--provider-delay', '4',
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        async for line in process.stdout:
                            yield json.dumps({**json.loads(line), 'label': label}) + '\n'
                        if await process.wait():
                            yield json.dumps({'error': 'Probe failed; inspect local server output.'}) + '\n'
                            print((await process.stderr.read()).decode(), file=sys.stderr)
                    finally:
                        if process.returncode is None:
                            process.terminate()
                            await process.wait()
        return StreamingResponse(output(), media_type='application/x-ndjson')

    uvicorn.run(app, host='127.0.0.1', port=args.serve)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--samples', type=int, default=10)
    parser.add_argument('--context-delay', type=float, default=.3)
    parser.add_argument('--provider-delay', type=float, default=.5)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--serve', type=int, help='Serve the live local comparison on this port')
    parser.add_argument('--baseline', type=Path, help='Baseline checkout for the browser comparison')
    args = parser.parse_args()
    if args.samples < 1 or not 0 <= args.context_delay <= 60 or not 0 <= args.provider_delay <= 60:
        parser.error('Use at least one sample and delays from 0 to 60 seconds')
    if args.serve and not args.baseline:
        parser.error('--serve requires --baseline')
    if args.serve:
        serve(args)
    else:
        asyncio.run(measure(args))
