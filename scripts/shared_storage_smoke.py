"""Real Postgres/CLI migration and a replacement process without local payloads.

Object requests use boto3 against a local S3-protocol HTTP fixture. This is not
a cloud-provider or production load test. Only a random test schema is changed.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.blob_storage import ObjectStorage
from app.config import Settings
from app.db import Store


@contextmanager
def object_service():
    values, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            digest = base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode()
            if (self.headers.get('Content-MD5') != digest
                    or not self.headers.get('Authorization', '').startswith('AWS4-HMAC-SHA256 ')):
                self.send_error(400)
                return
            values[self.path] = raw
            requests.append('PUT')
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            requests.append('GET')
            raw = values.get(self.path)
            self.send_response(200 if raw is not None else 404)
            self.send_header('Content-Length', str(len(raw or b'')))
            self.end_headers()
            if raw is not None:
                self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def command(environment, *arguments):
    result = subprocess.run([sys.executable, *arguments], env=environment, cwd=environment['DATA_DIR'],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Raw provider errors/environment values never enter the demonstration.
        raise RuntimeError('The isolated storage command failed; no success receipt was produced.')
    return json.loads(result.stdout)


def restored_reader():
    settings = Settings(_env_file=None)
    objects = ObjectStorage(settings)
    store = Store(settings.data_dir, object_storage=objects,
                  database_url=settings.moyai_database_url, database_schema=settings.moyai_database_schema,
                  application_instance=True)
    try:
        assert not store.artifacts.root.exists()
        row = store.rows('SELECT * FROM attachments')[0]
        assert row['data'] == row['preview'] == b''
        assert store.attachments.payload(row) == b'original upload'
        assert store.attachments.payload(row, 'preview') == b'image preview'
        expected = json.loads(os.environ['DEMO_ARTIFACTS'])
        for name, raw in expected.items():
            assert store.artifacts.read(name, 1024) == raw.encode('latin1')
        assert not store.path.exists()
        print(json.dumps({'restored_payloads': len(expected) + 2, 'local_artifact_directory': False,
                          'sqlite_created': False}))
    finally:
        store.close()


def main():
    logging.getLogger('uvicorn.error.moyai.scheduling').setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--read-restored', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.read_restored:
        restored_reader()
        return
    url = os.environ.get('MOYAI_TEST_POSTGRES_URL')
    if not url:
        parser.error('Set MOYAI_TEST_POSTGRES_URL to a disposable PostgreSQL database.')
    schema = 'moyai_storage_demo_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        version = conn.execute('SHOW server_version').fetchone()[0]
    try:
        with tempfile.TemporaryDirectory(prefix='moyai-storage-demo-') as temporary, object_service() as (endpoint, requests):
            root = Path(temporary)
            original = root / 'original'
            store = Store(original, database_url=url, database_schema=schema,
                          database_initialize=True, application_instance=True)
            try:
                run = store.create_run('Synthetic shared-file continuity', '', 'demo', [])['id']
                store.attachments.save(uuid4().hex, 'synthetic-owner', 'upload.png', b'original upload',
                                       ('image/png', b'image preview', ''), 1024)
                frozen = f'group-{uuid4().hex}-{run}-1.zip'
                payloads = {run + '.zip': 'workspace archive', frozen: 'frozen agent handoff',
                            run + '-captures/proof.png': '\x89PNG\r\n\x1a\nproof'}
                for name, value in payloads.items():
                    store.artifacts.save(name, value.encode('latin1'))
                store.execute('CREATE TABLE agent_groups(result_snapshot TEXT)')
                store.execute('INSERT INTO agent_groups VALUES(?)', (json.dumps([{'artifact_name': frozen}]),))
                store.path.write_bytes(b'old SQLite sentinel: never read or changed')
                environment = {
                    'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(ROOT), 'PYTHONUNBUFFERED': '1',
                    'DATA_DIR': str(original), 'MOYAI_DATABASE_URL': url, 'MOYAI_DATABASE_SCHEMA': schema,
                    'OBJECT_STORAGE_ENDPOINT': endpoint, 'OBJECT_STORAGE_BUCKET': 'synthetic-private',
                    'OBJECT_STORAGE_ACCESS_KEY_ID': 'synthetic-key', 'OBJECT_STORAGE_SECRET_ACCESS_KEY': 'synthetic-secret',
                    'DEMO_ARTIFACTS': json.dumps(payloads),
                }
                print(f'START   PostgreSQL {version}; live owner, one upload, preview and three local files.', flush=True)
                initial = command(environment, '-m', 'app.storage_maintenance', 'plan')
                assert initial['attachments']['pending'] == 1 and initial['legacy_artifacts']['pending'] == 3
                print('PLAN    Read-only CLI found all legacy files; the stale SQLite copy was ignored.', flush=True)
                first = command(environment, '-m', 'app.storage_maintenance', 'migrate', '--limit', '1')
                assert first['attachments_published'] == 1 and first['artifacts_published'] == 0
                second = command(environment, '-m', 'app.storage_maintenance', 'migrate')
                assert second['artifacts_published'] == 3
                store.update_run(run, summary='The existing application owner can still write')
                assert store.run(run)['summary'].startswith('The existing')
                print('MIGRATE Two CLI batches published five verified payloads; the app owner still writes.', flush=True)
                receipt = command(environment, '-m', 'app.storage_maintenance', 'verify')
                assert receipt['verified_objects'] == 5
                assert store.path.read_bytes() == b'old SQLite sentinel: never read or changed'
                assert all((store.artifacts.root / name).read_bytes() == raw.encode('latin1') for name, raw in payloads.items())
                print('VERIFY  Every object downloaded and checked; original disk files and SQLite preserved.', flush=True)
                cleared = command(environment, '-m', 'app.storage_maintenance', 'migrate', '--clear-attachment-blobs')
                assert cleared['attachment_bytes_cleared'] == len(b'original uploadimage preview')
            finally:
                store.close()
            replacement = root / 'replacement'
            replacement.mkdir()
            restored = command(environment | {'DATA_DIR': str(replacement)}, str(Path(__file__).resolve()), '--read-restored')
            assert restored['restored_payloads'] == 5 and not restored['local_artifact_directory']
            print('RESTORE New process on an empty disk read the upload, preview, archive, handoff and capture.', flush=True)
            report = {'postgres_version': version, 'initial': initial, 'verification': receipt,
                      'replacement': restored, 'http_puts': requests.count('PUT'), 'http_gets': requests.count('GET'),
                      'source_files_preserved': True, 'stale_sqlite_unchanged': True,
                      'scope': 'Local PostgreSQL and S3-protocol HTTP fixture; no production/provider validation.'}
            if args.output:
                args.output.write_text(json.dumps(report, indent=2) + '\n')
            print('PASS    Migration, resumability and replacement reads verified. No production changes.', flush=True)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


if __name__ == '__main__':
    main()
