"""Record actual local MCP/Git and Access-cache regressions with disposable data.

    uv run python -m scripts.access_regression_demo --output /tmp/moyai-regression-demo

Uses a local TLS gate, real MCP subprocesses and git-http-backend. Publication
metadata is supplied by a local provider fixture; no external PR is created.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    started, events, transcript = time.monotonic(), [], []

    def record(text):
        print(text, end='', flush=True)
        transcript.append(text)
        events.append([round(time.monotonic() - started, 3), 'o', text.replace('\n', '\r\n')])

    record('MOYAI | Real local transport and key-refresh regression recording\n')
    record('Synthetic credentials; local TLS gate and Git provider. No production changes.\n\n')
    cases = [
        ('SDK-sanitized MCP process: checkout, publication and revision fetch',
         'tests/test_access_transport.py::test_real_mcp_git_checkout_and_publication_through_access[False-True]'),
        ('Git boundary: binary protocol, credential scoping, rejected redirects and revoked access',
         'tests/test_access_transport.py::test_git_relay_preserves_binary_protocol_and_rejects_redirects'),
        ('Signing keys: failed refresh preserves cache, expiry fails closed, rotation recovers',
         'tests/test_cloudflare_access.py::test_bad_refresh_preserves_valid_cache_and_recovers[broken_keys0]'),
    ]
    for label, case in cases:
        record(label + '\n$ python -m pytest -q -s --tb=no -p no:warnings ' + case + '\n')
        time.sleep(2)
        command = [sys.executable, '-m', 'pytest', '-q', '-s', '--tb=no', '-p', 'no:warnings', case]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              cwd=Path(__file__).resolve().parents[1]) as child:
            for line in child.stdout:
                record(line)
            if child.wait() != 0:
                raise SystemExit('Regression failed; review the actual output above.')
        record('\n')
        time.sleep(2)
    record('All three actual regression commands passed.\n')
    header = {'version': 2, 'width': 110, 'height': 28, 'timestamp': int(time.time()),
              'title': 'Moyai Access regression recording'}
    (output / 'recording.cast').write_text('\n'.join(json.dumps(row) for row in [header, *events]) + '\n')
    (output / 'verification.txt').write_text(''.join(transcript))
    page = '''<!doctype html><meta charset="utf-8"><title>Moyai · Access regression recording</title>
<style>body{background:#10141d;color:#e3e8f4;font:17px system-ui;margin:25px auto;max-width:1250px;padding:0 25px}h1{font-size:30px}p{color:#adb8cd}button{background:#6653da;color:white;border:0;padding:12px 20px;border-radius:8px;font-size:15px;margin-right:10px;cursor:pointer}pre{background:#090c13;border:1px solid #313b51;border-radius:12px;padding:20px;font:14px/1.55 ui-monospace,monospace;white-space:pre-wrap;color:#b9e2d0}#progress{margin-left:14px}</style>
<h1>Moyai · Access regression verification</h1><p>Actual local MCP subprocess, Git clone/fetch and signing-key tests. Disposable data and a local TLS gate.</p>
<button id="play">Replay recording</button><button id="end">Show results</button><span id="progress"></span><pre id="terminal"></pre>
<script>const events=EVENTS;let started,frame;const terminal=document.getElementById('terminal'),progress=document.getElementById('progress');function show(t){terminal.textContent=events.filter(e=>e[0]<=t).map(e=>e[2]).join('');progress.textContent=Math.min(t,events.at(-1)[0]).toFixed(1)+' s / '+events.at(-1)[0].toFixed(1)+' s';}function tick(now){let t=(now-started)/1000;show(t);if(t<events.at(-1)[0])frame=requestAnimationFrame(tick);}document.getElementById('play').onclick=()=>{cancelAnimationFrame(frame);started=performance.now();frame=requestAnimationFrame(tick);};document.getElementById('end').onclick=()=>{cancelAnimationFrame(frame);show(Infinity);};show(Infinity);</script>'''
    (output / 'recording.html').write_text(page.replace('EVENTS', json.dumps(events).replace('<', '\\u003c')))
    (output / 'README.txt').write_text(__doc__ + '\nOpen recording.html to replay the actual timestamped output.\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    main(parser.parse_args().output)
