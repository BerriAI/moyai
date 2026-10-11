"""Compare tool-image counting with a baseline and exercise the real budget check.

Run: python -m scripts.tool_image_budget_demo --baseline COMMIT
Uses a generated PNG and a local HTTP counter stub, with no provider calls.
The token total is simulated; extraction, usage anchoring, HTTP and errors are real.
"""
import argparse
import ast
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import random
import subprocess
import threading
import time
from types import SimpleNamespace

from fastapi import HTTPException
from PIL import Image

from app.context_budget import ContextBudget, ModelContextLimits, counting_input


def baseline_counter(commit):
    source = subprocess.run(['git', 'show', f'{commit}:app/context_budget.py'],
                            check=True, capture_output=True, text=True).stdout
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'counting_input')
    namespace = {'json': json, 'HTTPException': HTTPException}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<baseline counting_input>', 'exec'), namespace)
    return namespace['counting_input']


async def demonstrate(baseline, delay=0):
    def say(message):
        print(message, flush=True)
        if delay:
            time.sleep(delay)

    # A deterministic, valid PNG large enough to expose the base64-as-text bug.
    png = BytesIO()
    Image.frombytes('RGB', (640, 480), random.Random(0).randbytes(640 * 480 * 3)).save(png, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()
    image = {'type': 'input_image', 'image_url': url, 'detail': 'high'}
    outputs = {
        'message': {'role': 'user', 'content': [image]},
        **{kind: {'type': kind, 'call_id': 'demo-image', 'output': [image]}
           for kind in ('function_call_output', 'custom_tool_call_output')},
    }
    before = baseline_counter(baseline)
    report = {'baseline': baseline, 'scope': 'real counting/budget code; local HTTP counter with simulated token total',
              'png_bytes': len(png.getvalue()), 'placements': []}
    say('TOOL IMAGE CONTEXT BUDGET — real code, generated PNG, local counter stub')
    say(f'Same 640 x 480 PNG in each request: {len(png.getvalue()):,} bytes; detail=high.')
    say('Placement                  Before: text bytes / images   After: text bytes / images')
    for name, output in outputs.items():
        payload = {'input': [output]}
        old_messages, old_bytes, old_image = before(payload)
        new_messages, new_bytes, new_image = counting_input(payload)
        assert new_image and new_messages[0]['content'][1]['image_url'] == {'url': url, 'detail': 'high'}
        assert url not in new_messages[0]['content'][0]['text']
        if name != 'message':
            assert not old_image and old_bytes > 714600
        say(f'{name:<26} {old_bytes:>10,} / {int(old_image):<6}         {new_bytes:>6,} / {int(new_image)}')
        report['placements'].append({'placement': name, 'before_text_bytes': old_bytes,
                                     'before_images': int(old_image), 'after_text_bytes': new_bytes, 'after_images': int(new_image)})

    calls = []
    mode = 'verified'

    class Counter(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            content = body['messages'][0]['content']
            assert self.path == '/utils/token_counter?call_endpoint=true'
            assert content[1]['image_url'] == {'url': url, 'detail': 'high'}
            assert len(content[0]['text']) < 400
            calls.append(mode)
            response = {'total_tokens': 110000,
                        'tokenizer_type': 'openai_api' if mode == 'verified' else 'openai_tokenizer'}
            raw = json.dumps(response).encode()
            self.send_response(503 if mode == 'outage' else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Counter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = SimpleNamespace(litellm_api_base=f'http://127.0.0.1:{server.server_port}/v1',
                                   litellm_api_key='local-demo', model_context_limits={'demo': ModelContextLimits(
                                       context_window=922000, max_input_tokens=922000, max_output_tokens=128000)})
        budget = ContextBudget(settings)
        original = {'model': 'demo', 'input': [{'role': 'user', 'content': 'Inspect the screenshot.'}]}
        budget.remember(original, {'prompt_tokens': 109182}, 'demo-run')
        payload = {**original, 'input': [*original['input'], outputs['custom_tool_call_output']]}
        original_wire = json.dumps(payload)
        result = await budget.check(payload, scope='demo-run')
        assert result.input_tokens == 110000 and result.input_budget == 714600
        assert json.dumps(payload) == original_wire
        say(f'PASS: appended tool image uses HTTP counter: {result.input_tokens:,} tokens < {result.input_budget:,} budget.')
        say('PASS: inference input is unchanged, including custom tool identity and image detail.')
        report['budget'] = result.public()
        for mode in ('outage', 'generic'):
            try:
                await budget.check(payload, scope='demo-run')
            except HTTPException as exc:
                assert exc.status_code == 503
                say(f'PASS: {mode} counter returns HTTP 503; no text-only fallback for a new image.')
            else:
                raise AssertionError('Unverified image count was admitted')
        budget.remember(payload, {'prompt_tokens': 110000}, 'demo-run')
        followup = {**payload, 'input': [*payload['input'], {'role': 'user', 'content': 'Explain the screenshot.'}]}
        result = await budget.check(followup, scope='demo-run')
        assert result.method == 'usage_plus_utf8_delta' and len(calls) == 3
        say('PASS: unchanged image prefix uses known usage; text follow-up needs no new counter call.')
        report.update(counter_calls=calls, fail_closed=True, inference_input_unchanged=True, unchanged_prefix_reuses_usage=True)
        say('DONE: extraction and failure behavior verified locally. No production or provider requests.')
        return report
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, help='Git commit containing the original counting_input implementation')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--delay', type=float, default=0, help='Seconds between output lines for a readable live recording')
    args = parser.parse_args()
    if not 0 <= args.delay <= 3:
        parser.error('Use a delay from 0 to 3 seconds')
    result = asyncio.run(demonstrate(args.baseline, args.delay))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
