"""Real boto3 HTTP requests plus manifest publication and recovery boundaries."""
import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from fastapi import HTTPException
import pytest

from app.blob_storage import ObjectStorage
from app.config import Settings
from app.db import Store
from app.persistence import Checkpoints, restore_checkpoint
from storage_fixture import MemoryObjects


@pytest.fixture
def service():
    values, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            assert self.headers['Authorization'].startswith('AWS4-HMAC-SHA256 ')
            assert self.headers['Content-MD5'] == base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode()
            requests.append(('PUT', self.path))
            values[self.path] = raw
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            requests.append(('GET', self.path))
            raw = values.get(self.path)
            self.send_response(200 if raw is not None else 404)
            self.send_header('Content-Length', str(len(raw or b'')))
            self.end_headers()
            if raw is not None:
                self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(_env_file=None, object_storage_bucket='test-private-bucket',
                        object_storage_endpoint=f'http://127.0.0.1:{server.server_port}',
                        object_storage_access_key_id='synthetic-key', object_storage_secret_access_key='synthetic-secret')
    backend = ObjectStorage(settings)
    try:
        yield backend, values, requests
    finally:
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_signed_sdk_transport_and_streamed_backup_verify(service, tmp_path):
    backend, values, requests = service
    raw = b'payload\x00' * 300000
    reference = backend.put(raw)
    assert backend.read(reference, len(raw)) == raw
    source = tmp_path / 'backup'
    source.write_bytes(raw)
    assert backend.put_file(source) == reference
    backend.verify(reference, len(raw))
    assert len(values) == 1 and [verb for verb, _ in requests] == ['PUT', 'GET', 'PUT', 'GET']
    with pytest.raises(HTTPException, match='could not be retrieved'):
        backend.verify(reference, len(raw) + 1)
    with pytest.raises(HTTPException):
        backend.read(reference, len(raw) - 1)
    key = next(iter(values))
    values[key] = b'x' * len(raw)
    with pytest.raises(HTTPException, match='could not be retrieved'):
        backend.read(reference, len(raw))
    del values[key]
    with pytest.raises(HTTPException, match='could not be retrieved'):
        backend.read(reference, len(raw))


def test_destination_binding_never_requests_another_bucket(service):
    backend, _, requests = service
    reference = backend.put(b'private')
    changed = ObjectStorage(backend.settings.model_copy(update={'object_storage_bucket': 'other-private-bucket'}))
    for selected in (ObjectStorage(), changed):
        with pytest.raises(HTTPException, match='original private storage configuration'):
            selected.read(reference, 100)
    assert [verb for verb, _ in requests] == ['PUT']


@pytest.mark.parametrize('fields', [
    {'object_storage_access_key_id': 'partial'},
    {'object_storage_session_token': 'partial'},
    {'object_storage_endpoint': 'http://storage.example'},
    {'object_storage_endpoint': 'https://user:secret@storage.example'},
    {'object_storage_prefix': '../outside'},
])
def test_invalid_destination_rejected_before_client_creation(fields):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **fields)


def test_upload_publication_failure_shadowing_and_snapshots(tmp_path):
    backend = MemoryObjects()
    store = Store(tmp_path, object_storage=backend)
    legacy = tmp_path / 'artifacts'
    legacy.mkdir()
    (legacy / 'latest.zip').write_bytes(b'old')
    backend.fail = True
    with pytest.raises(HTTPException):
        store.artifacts.save('latest.zip', b'new')
    assert not store.rows('SELECT * FROM artifact_objects')
    assert store.artifacts.read('latest.zip', 100) == b'old'
    backend.fail = False
    store.artifacts.save('latest.zip', b'new')
    revision = store.artifacts.info('latest.zip')['revision']
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.artifacts.snapshot_in(conn, 'latest.zip', 'frozen.zip')
    store.artifacts.save('latest.zip', b'newer')
    assert store.artifacts.read('frozen.zip', 100) == b'new'
    with pytest.raises(HTTPException) as stale:
        store.artifacts.read('latest.zip', 100, revision=revision)
    assert stale.value.status_code == 409
    (legacy / 'latest.zip').unlink()
    (legacy / 'latest.zip').symlink_to('/outside')
    assert store.artifacts.read('latest.zip', 100) == b'newer'
    backend.fail = True
    reads = backend.reads
    assert store.artifacts.info('latest.zip')['size'] == 5
    assert backend.reads == reads
    with pytest.raises(HTTPException):
        store.artifacts.read('latest.zip', 100)
    reopened = Store(tmp_path)
    with pytest.raises(HTTPException):
        reopened.artifacts.save('latest.zip', b'rollback')
    assert (legacy / 'latest.zip').is_symlink()


def test_batched_availability_matches_metadata_without_reading_objects(tmp_path):
    backend = MemoryObjects()
    store = Store(tmp_path, object_storage=backend)
    store.artifacts.save('remote.zip', b'remote archive')
    legacy = tmp_path / 'artifacts'
    legacy.mkdir(exist_ok=True)
    (legacy / 'local.zip').write_bytes(b'legacy archive')
    (legacy / 'directory.zip').mkdir()
    (legacy / 'linked.zip').symlink_to(legacy / 'local.zip')
    (legacy / 'linked-directory').symlink_to(legacy, target_is_directory=True)
    names = ['remote.zip', 'local.zip', 'absent.zip', 'directory.zip',
             'linked.zip', 'linked-directory/local.zip']
    before = backend.reads
    assert store.artifacts.available_names(names) == {
        name for name in names if store.artifacts.info(name) is not None
    } == {'remote.zip', 'local.zip'}
    assert backend.reads == before
    assert store.artifacts.available_names([]) == set()
    with pytest.raises(ValueError):
        store.artifacts.available_names(['../outside.zip'])


async def test_checkpoint_restores_remote_manifest_without_copying_payload(tmp_path):
    backend = MemoryObjects()
    settings = Settings(_env_file=None, data_dir=tmp_path / 'original', checkpoint_dir=tmp_path / 'checkpoint')
    store = Store(settings.data_dir, object_storage=backend)
    store.artifacts.save('archive.zip', b'private archived bytes')
    async def commit():
        pass
    await Checkpoints(store, settings, commit=commit).flush()
    settings.data_dir = tmp_path / 'restored'
    restore_checkpoint(settings)
    restored = Store(settings.data_dir, object_storage=backend)
    assert restored.artifacts.read('archive.zip', 100) == b'private archived bytes'
    assert not (settings.data_dir / 'artifacts' / 'archive.zip').exists()


async def test_download_temp_is_removed_even_when_response_is_cancelled(tmp_path):
    store = Store(tmp_path)
    store.artifacts.save('archive.zip', b'zip data')
    response = store.artifacts.download('archive.zip', 'download.zip')
    temporary = Path(response.path)
    assert temporary.exists() and not temporary.is_relative_to(tmp_path)

    async def cancelled(message):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await response({'type': 'http', 'method': 'GET', 'headers': []}, None, cancelled)
    assert not temporary.exists()


def test_download_refuses_temporary_files_on_database_directory(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.artifacts.save('archive.zip', b'zip data')
    monkeypatch.setattr('app.blob_storage.tempfile.tempdir', str(tmp_path))
    with pytest.raises(HTTPException, match='outside the database directory'):
        store.artifacts.download('archive.zip', 'download.zip')
    assert not list(tmp_path.glob('moyai-download-*'))
