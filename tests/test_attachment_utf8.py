"""Preview bounds must not turn valid UTF-8 uploads into binary files."""
import pytest
from uuid import uuid4
from test_workspace import workspace
from app.attachments import inspect_file

@pytest.mark.parametrize('character', ['é', '€', '😀'])
@pytest.mark.parametrize('offset', [15997, 15998, 15999, 16000])
def test_utf8_preview_boundary(workspace, character, offset):
    _, client = workspace
    header = 'Région,売上,Notes\nNord,1,'.encode()
    raw = header + b'a' * (offset - len(header)) + (character + '\n').encode()
    response = client.put('/api/attachments/' + uuid4().hex,
        params={'name': 'sales.csv'}, content=raw,
        headers={'Content-Type': 'application/octet-stream'})
    assert response.status_code == 200
    file = response.json()
    assert file['media_type'] == 'text/plain'
    assert file['preview_text'].startswith('Région,売上,Notes')
    assert len(file['preview_text']) <= 4000
    assert client.get(file['url']).content == raw

@pytest.mark.parametrize('raw', [b'abc\xff', b'abc\xc3', b'a' * 15999 + b'\xc3', b'abc\x00def', b'a' * 15998 + b'\xff' + b'a' * 5])
def test_invalid_or_binary_prefix_not_previewed(raw):
    assert inspect_file(raw) == ('application/octet-stream', b'', '')
