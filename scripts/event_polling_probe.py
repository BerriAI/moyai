"""Measure an actual local SSE connection; no providers or production data.

Run with ``uv run python -m scripts.event_polling_probe --output result.json``.
``--database-url`` uses a disposable PostgreSQL schema and drops it afterward.
The server runs without background workers so only the stream owns measured reads.
"""
import argparse
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn

from app.config import Settings


def app_factory():
    if 'app.main' not in sys.modules:
        # main.py creates its default app during import. Keep that unused app
        # away from the caller's .env, shell credentials, and configured store.
        with tempfile.TemporaryDirectory(prefix='moyai-poll-import-') as directory:
            previous = Path.cwd()
            with patch.dict(os.environ, {'DATA_DIR': directory, 'BUNDLED_SKILLS_ENABLED': 'false'}, clear=True):
                try:
                    os.chdir(directory)
                    module = importlib.import_module('app.main')
                finally:
                    os.chdir(previous)
            module.app.state.store.close()
    return importlib.import_module('app.main').create_app


class Reads:
    def __init__(self, store):
        self.active = False
        self.checkouts = 0
        self.statements = []
        self.connect = store.connect

    @contextmanager
    def measured(self, **kwargs):
        if self.active:
            self.checkouts += 1
        with self.connect(**kwargs) as connection:
            yield self.Connection(connection, self)

    class Connection:
        def __init__(self, connection, reads):
            self.connection, self.reads = connection, reads

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, params=()):
            if self.reads.active and statement.lstrip().upper().startswith(('SELECT', 'WITH')):
                self.reads.statements.append(statement)
            return self.connection.execute(statement, params)


def records(response):
    record = {}
    for line in response.iter_lines():
        if not line:
            if record:
                yield record
                record = {}
        elif ': ' in line:
            name, value = line.split(': ', 1)
            record[name] = json.loads(value) if name == 'data' else value


def probe(database_url='', polls=6):
    schema = 'moyai_poll_probe_' + uuid4().hex
    if database_url:
        import psycopg
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{schema}"')
    app, thread, server = None, None, None
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-poll-probe-') as directory:
            # Explicit defaults prevent shell credentials, auth, storage, or
            # runtime settings from changing the isolated local demonstration.
            values = {name: field.get_default(call_default_factory=True) for name, field in Settings.model_fields.items()}
            values.update(data_dir=Path(directory), public_url=url,
                temporal_enabled=True, bundled_skills_enabled=False, repository_context_enabled=False,
                moyai_database_url=database_url, moyai_database_schema=schema, moyai_database_initialize=True)
            app = app_factory()(Settings(_env_file=None, **values))
            store = app.state.store
            run = store.create_run('Synthetic saved conversation', '', 'demo', [], chat_enabled=True)
            store.update_run(run['id'], status='idle')
            reads = Reads(store)
            store.connect = reads.measured
            server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]})
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started, 'Local server did not start'
            path = '/api/runs/' + run['id'] + '/events'
            with httpx.Client(base_url=url, timeout=5) as client:
                assert client.get('/api/session').status_code == 200
                with client.stream('GET', path) as response:
                    assert response.status_code == 200
                    stream = records(response)
                    while next(stream).get('event') != 'run-status':
                        pass
                    reads.active = True
                    started = time.monotonic()
                    for _ in range(polls):
                        assert next(stream)['event'] == 'run-status'
                    elapsed = time.monotonic() - started
                    reads.active = False
                    measured = {'polls': polls, 'database_checkouts': reads.checkouts,
                        'select_statements': len(reads.statements), 'elapsed_seconds': round(elapsed, 3),
                        'checkouts_per_poll': reads.checkouts / polls,
                        'selects_per_poll': len(reads.statements) / polls}
                    # Independent committed updates remain visible on the next poll.
                    store.update_run(run['id'], status='running')
                    store.event(run['id'], 'tool', 'Fresh event from another connection')
                    started = time.monotonic()
                    event = next(stream)
                    assert event['data']['message'] == 'Fresh event from another connection'
                    event_id = int(event['id'])
                    assert next(stream)['data']['active'] is True
                    update_ms = (time.monotonic() - started) * 1000
                store.event(run['id'], 'tool', 'Reconnect continuation')
                with client.stream('GET', path, headers={'Last-Event-ID': str(event_id)}) as response:
                    resumed = next(records(response))
                    assert resumed['data']['message'] == 'Reconnect continuation'
                    assert int(resumed['id']) > event_id
            return {'backend': 'postgresql' if database_url else 'sqlite', 'transport': 'real local HTTP SSE',
                'runner': type(app.state.manager).__name__, 'idle': measured,
                'fresh_update_ms': round(update_ms, 1), 'reconnect_cursor': 'passed',
                'scope': 'Synthetic local stream; not a production capacity or navigation benchmark.'}
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
            assert not thread.is_alive(), 'Local server did not stop'
        sock.close()
        if app:
            app.state.store.close()
        if database_url:
            with psycopg.connect(database_url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default='')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = probe(args.database_url)
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n')


if __name__ == '__main__':
    main()
