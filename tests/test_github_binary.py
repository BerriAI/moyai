"""Complete publisher payloads preserve old receipt identities and binary bytes."""
import asyncio
import base64
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.github import Change, Publish
from test_github import connected, GitHubAPI, PAYLOAD
from test_workspace import workspace


def test_text_payload_serialization_preserves_existing_receipt_hashes():
    args = Publish.model_validate(PAYLOAD)
    old_shape = {**args.model_dump(), 'files': [{'path': file.path, 'content': file.content, 'executable': file.executable}
                                                for file in args.files]}
    assert args.model_dump() == old_shape
    # Both key order for legacy receipts and sorted canonical hashes stay stable.
    assert args.model_dump_json() == json.dumps(old_shape, separators=(',', ':'), ensure_ascii=False)


@pytest.mark.parametrize('content', ['invalid base64', 'AB==', '你好', None])
def test_binary_encoding_has_one_canonical_validated_payload(content):
    with pytest.raises(ValidationError):
        Change(path='image.bin', content=content, encoding='base64')


def test_many_files_and_binary_publish_once_after_lost_ack(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    raw = bytes(range(256)) * 13
    files = [{'path': f'source/{i}.txt', 'content': f'Complete file {i}'} for i in range(105)]
    files.append({'path': 'images/example.bin', 'content': base64.b64encode(raw).decode(), 'encoding': 'base64'})
    args = Publish.model_validate({**PAYLOAD, 'files': files})
    api.lose = 'pr'
    first = asyncio.run(github.publish(app.state.store.run(run_id), args))
    result = asyncio.run(github.publish(app.state.store.run(run_id), args))
    assert result == first and result['number'] == 100
    blobs = [call[2]['json'] for call in api.calls if call[0] == 'POST' and call[1].endswith('/git/blobs')]
    assert len(blobs) == 106
    binary = next(blob for blob in blobs if blob['encoding'] == 'base64')
    assert hashlib.sha256(base64.b64decode(binary['content'])).digest() == hashlib.sha256(raw).digest()
    assert sum(method == 'POST' and path.endswith('/pulls') for method, path, _ in api.calls) == 1
    assert len(app.state.store.rows('SELECT * FROM github_publications')) == 1
