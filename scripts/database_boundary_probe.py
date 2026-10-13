"""Exercise duplicate input through a running, authenticated local Moyai app.

Start the actual app with an isolated PostgreSQL schema and
MOYAI_DATABASE_POOL_SIZE=1. Set MOYAI_PROBE_PASSWORD privately, then run:
  python scripts/database_boundary_probe.py --url http://127.0.0.1:8787

The built-in session-ID command needs no model or sandbox; this measures intake
and receipt replay, not inference. Two test sessions remain in the local app.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


async def probe(url):
    parsed = urlsplit(url)
    if parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Use an isolated local app for this probe.')
    body = {'prompt': '/session-id', 'mode': 'modal', 'plugins': [], 'chat_enabled': True,
            'client_id': 'db-boundary-' + uuid4().hex, 'harness': 'hermes',
            'model': 'openai/gpt-6-astra'}
    async with httpx.AsyncClient(base_url=url, timeout=30, headers={'Origin': url}) as client:
        response = await client.post('/api/login', json={'password': os.environ['MOYAI_PROBE_PASSWORD']})
        response.raise_for_status()
        session = await client.get('/api/session')
        session.raise_for_status()
        client.headers['X-CSRF-Token'] = session.json()['csrf']

        async def submit(label, value):
            print(f'{label}: POST /api/runs', flush=True)
            started = time.monotonic()
            response = await client.post('/api/runs', json=value)
            elapsed = time.monotonic() - started
            try:
                data = response.json()
            except ValueError:
                data = {'detail': 'Non-JSON error response'}
            print(f'{label}: HTTP {response.status_code} in {elapsed:.3f}s'
                  + (f", session {data['id']}" if 'id' in data else ''), flush=True)
            return {'http_status': response.status_code, 'seconds': round(elapsed, 3),
                    'run_id': data.get('id'), 'error': data.get('detail')}

        first = await submit('Initial input', body)
        if first['http_status'] != 201:
            raise RuntimeError('Initial input was rejected.')
        print('Replay the identical input while another session starts.', flush=True)
        replay = asyncio.create_task(submit('Same request ID', body))
        await asyncio.sleep(.2)
        peer = asyncio.create_task(submit('Independent session',
            {**body, 'client_id': 'db-boundary-' + uuid4().hex}))
        duplicate, unrelated = await asyncio.gather(replay, peer)
        response = await client.get('/api/runs/' + first['run_id'])
        response.raise_for_status()
        details = response.json()
        result = {'initial': first, 'duplicate': duplicate, 'independent': unrelated,
                  'same_session': duplicate['run_id'] == first['run_id'],
                  'user_messages': sum(message['role'] == 'user' for message in details['messages']),
                  'assistant_messages': sum(message['role'] == 'assistant' for message in details['messages']),
                  'model_calls': details['model_calls']}
        print('Durable receipt: ' + json.dumps({key: result[key] for key in
              ('same_session', 'user_messages', 'assistant_messages', 'model_calls')}), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = asyncio.run(probe(args.url.rstrip('/')))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    if not (result['same_session'] and result['user_messages'] == result['assistant_messages'] == 1
            and result['model_calls'] == 0 and result['duplicate']['seconds'] < 2
            and result['independent']['http_status'] == 201 and result['independent']['seconds'] < 2):
        raise SystemExit('Database boundary check failed.')
    print('PASS: replay reused its receipt; independent intake stayed responsive.', flush=True)


if __name__ == '__main__':
    main()
