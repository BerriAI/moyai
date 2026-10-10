import base64
import hashlib
import json

import pytest
from fastapi import HTTPException

from app.broker_payloads import BrokerPayloads
from sandbox.broker_transport import CONTENT_TYPE, MAX_BODY, seal, send_payload
from test_workspace import workspace
from test_context_gateway import native_exchange, native_followup
from test_spend import active


def test_large_native_state_upload_resume_and_owner_fences(workspace):
    app, client = workspace
    run = active(app)
    assert native_exchange(client, run['id'], 'begin').json()['reason'] == 'missing'
    state = {'transcript': 'PRIVATE LARGE STATE 漢🙂' * 250000}
    body = {'action': 'commit', 'lease': 'a'*32, 'compatibility': 'b'*64,
            'checkpoint': {'epoch': 'c'*32, 'seq': 7}, 'state': state}
    requests = []
    def send(route, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        assert len(raw) <= MAX_BODY
        requests.append((route, value))
        response = client.post('/broker/'+run['id']+route, content=seal('capability', route, raw),
            headers={'Authorization': 'Bearer capability', 'Content-Type': CONTENT_TYPE})
        assert response.status_code == 200, response.text
        return response.json()
    assert send_payload('/context/native', body, send)['saved']
    assert len(requests) > 2
    # A lost last acknowledgment resends the same immutable parts and commit.
    assert send_payload('/context/native', body, send)['saved']
    service = BrokerPayloads(app.state.store, app.state.security)
    manifest = requests[-1][1]
    assert service.resolve(run, '/context/native', manifest) == body
    with pytest.raises(HTTPException, match='unavailable'):
        service.resolve(run, '/agent-runtime', manifest)
    serialized = json.dumps(app.state.store.rows('SELECT * FROM broker_payload_parts'))
    assert 'PRIVATE LARGE STATE' not in serialized
    current = native_followup(app, run)
    with pytest.raises(HTTPException, match='owner changed'):
        service.resolve(run, '/context/native', manifest)
    with pytest.raises(HTTPException, match='unavailable'):
        service.resolve(current, '/context/native', manifest)
    result = native_exchange(client, run['id'], 'begin', 'd'*32, token='next-capability').json()
    assert result['reason'] == 'resumed' and result['state'] == state
    assert 'PRIVATE LARGE STATE' not in json.dumps(app.state.store.events(run['id']))


def test_payload_integrity_completeness_and_deduplication(workspace):
    app, _ = workspace
    run = active(app)
    service = BrokerPayloads(app.state.store, app.state.security)
    raw = json.dumps({'name': 'computer_write_file', 'arguments': {'text': 'complete'}}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    first = {'route': '/agent-runtime', 'digest': digest, 'part': 0,
             'data': base64.b64encode(raw[:10]).decode()}
    manifest = {'moyai_payload': {'digest': digest, 'parts': 2}}
    assert service.upload(run, first) == service.upload(run, first)
    with pytest.raises(HTTPException, match='different bytes'):
        service.upload(run, {**first, 'data': base64.b64encode(b'changed').decode()})
    with pytest.raises(HTTPException, match='unavailable'):
        service.resolve(run, '/agent-runtime', manifest)
    service.upload(run, {**first, 'part': 1, 'data': base64.b64encode(raw[10:]).decode()})
    assert service.resolve(run, '/agent-runtime', manifest) == json.loads(raw)
    bad = '0'*64
    service.upload(run, {**first, 'digest': bad, 'data': base64.b64encode(raw).decode()})
    with pytest.raises(HTTPException, match='integrity'):
        service.resolve(run, '/agent-runtime', {'moyai_payload': {'digest': bad, 'parts': 1}})
