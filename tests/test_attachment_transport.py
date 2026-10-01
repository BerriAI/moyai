import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from app.attachment_transport import CONTENT_TYPE
from app.attachments import MAX_FILE
from test_spend import sign_in
from test_workspace import workspace


def packet(csrf, attachment_id, name, raw, stamp=None):
    stamp = int(time.time()) if stamp is None else stamp
    nonce = os.urandom(12)
    target = f'{CONTENT_TYPE}\0{attachment_id}\0{name}\0{stamp}'.encode()
    key = hashlib.sha256(b'moyai-attachment-v1\0' + csrf.encode()).digest()
    return struct.pack('>Q', stamp) + nonce + AESGCM(key).encrypt(nonce, raw, target)


def upload(client, attachment_id, name, data, **headers):
    return client.put('/api/attachments/' + attachment_id, params={'name': name}, content=data,
                      headers={'Content-Type': CONTENT_TYPE, **headers})


def test_browser_webcrypto_upload_roundtrip_preserves_original_and_retry_identity(workspace):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to exercise real browser WebCrypto serialization')
    app, client = workspace
    attachment_id, name = uuid4().hex, 'référence runbook.md'
    raw = ('# Code examples are reference data\n```sql\nSELECT * FROM tasks;\n```\n' * 6000).encode()
    script = Path('app/static/attachment-transport.js').read_text() + '''
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
sealAttachment(new Blob([Buffer.from(input.data,'base64')]),input.id,input.name,input.csrf)
  .then(packet=>process.stdout.write(Buffer.from(packet).toString('base64')));
'''
    result = subprocess.run([node, '-e', script], input=json.dumps({
        'data': base64.b64encode(raw).decode(), 'id': attachment_id, 'name': name,
        'csrf': client.headers['X-CSRF-Token'],
    }), text=True, capture_output=True, check=True)
    sealed = base64.b64decode(result.stdout)
    assert b'SELECT * FROM tasks' not in sealed
    response = upload(client, attachment_id, name, sealed)
    assert response.status_code == 200, response.text
    file = response.json()
    assert file['name'] == name and file['size'] == len(raw)
    assert file['preview_text'].startswith('# Code examples')
    assert client.get(file['url']).content == raw
    # A lost response can be retried with a new envelope and the same upload ID.
    retry = packet(client.headers['X-CSRF-Token'], attachment_id, name, raw)
    assert retry != sealed
    assert upload(client, attachment_id, name, retry).json() == file
    assert len(app.state.store.rows('SELECT id FROM attachments')) == 1


@pytest.mark.parametrize('damage', ['contents', 'id', 'name', 'key', 'expired', 'future', 'truncated'])
def test_invalid_envelopes_never_store_files(workspace, damage):
    app, client = workspace
    attachment_id, name = uuid4().hex, 'runbook.md'
    stamp = int(time.time()) + {'expired': -301, 'future': 301}.get(damage, 0)
    data = packet('wrong-session' if damage == 'key' else client.headers['X-CSRF-Token'], attachment_id, name, b'# Data', stamp)
    if damage == 'contents':
        data = data[:-1] + bytes([data[-1] ^ 1])
    if damage == 'truncated':
        data = data[:16]
    response = upload(client, uuid4().hex if damage == 'id' else attachment_id,
                      'different.md' if damage == 'name' else name, data)
    assert response.status_code == 422, response.text
    assert not app.state.store.rows('SELECT id FROM attachments')


def test_session_and_csrf_are_checked_before_decryption(workspace, monkeypatch):
    _, client = workspace
    def must_not_decrypt(*args):
        raise AssertionError('Unauthenticated upload reached decryption')
    monkeypatch.setattr('app.attachments.unseal_file', must_not_decrypt)
    assert upload(client, uuid4().hex, 'runbook.md', b'junk', **{'X-CSRF-Token': ''}).status_code == 403
    client.cookies.clear()
    assert upload(client, uuid4().hex, 'runbook.md', b'junk').status_code == 401


def test_other_session_cannot_replay_a_valid_envelope(workspace):
    app, client = workspace
    sign_in(app, client)
    attachment_id = uuid4().hex
    data = packet(client.headers['X-CSRF-Token'], attachment_id, 'runbook.md', b'# private')
    sign_in(app, client, 'ishaan', 'ishaan@berri.ai')
    assert upload(client, attachment_id, 'runbook.md', data).status_code == 422
    assert not app.state.store.rows('SELECT id FROM attachments')


def test_wire_overhead_does_not_change_original_size_and_storage_limits(workspace, monkeypatch):
    app, client = workspace
    attachment_id, name = uuid4().hex, 'maximum.txt'
    raw = b'x' * MAX_FILE
    data = packet(client.headers['X-CSRF-Token'], attachment_id, name, raw)
    response = upload(client, attachment_id, name, data)
    assert response.status_code == 200, response.text
    assert response.json()['size'] == MAX_FILE
    assert client.get(response.json()['url']).content == raw
    assert upload(client, uuid4().hex, name, data + b'x').status_code == 413
    client.delete(response.json()['url'])
    monkeypatch.setattr('app.attachments.MAX_DRAFT', 2)
    assert upload(client, attachment_id, name, data).status_code == 422
    assert not app.state.store.rows('SELECT id FROM attachments')
