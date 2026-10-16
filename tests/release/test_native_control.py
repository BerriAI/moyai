"""Native maintenance command validation and single-shot transport boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release import native_control as control
from scripts.release.deploy import ReleaseError


@pytest.fixture
def body():
    return {
        'protocol': 1, 'core': 'pydantic-v1', 'native_schema': 'native_fixture', 'task_queue': 'native-fixture-v1',
        'expected_generation': 7, 'request_key': 'release:fixture:pause',
        'expected_worker': {'pid': 42, 'backend_start': '2026-10-15T00:00:00+00:00',
                            'source_policy': 'a' * 64, 'worker_policy': 'b' * 64},
    }


def receipt(body, command):
    return {
        **{key: body[key] for key in ('protocol', 'core', 'native_schema', 'task_queue')},
        'generation': body['expected_generation'] + 1, 'paused': command == 'request',
        'request_key': body['request_key'], 'request_fingerprint': control.fingerprint(body, paused=command == 'request'),
        'requester': {key: body['expected_worker'][key] for key in ('pid', 'backend_start')},
        'source_policy': body['expected_worker']['source_policy'],
    }


@pytest.mark.parametrize('command', ['request', 'resume'])
@pytest.mark.parametrize('fault', [
    None, 'generation', 'paused', 'request_fingerprint', 'source_policy', 'native_schema', 'task_queue',
    'request_key', 'requester', 'extra', 'protocol_bool',
])
def test_write_receipt_binds_generation_worker_scope_and_intent(body, command, fault):
    value = receipt(body, command)
    if fault == 'generation': value['generation'] += 1
    elif fault == 'paused': value['paused'] = not value['paused']
    elif fault == 'requester': value['requester']['pid'] = 99
    elif fault == 'extra': value['unexpected'] = True
    elif fault == 'protocol_bool': value['protocol'] = True
    elif fault: value[fault] = 'different'
    if fault:
        with pytest.raises(ValueError):
            control.validate(value, body, command)
    else:
        assert control.validate(value, body, command) == value


def test_status_cannot_claim_drained_with_unknown_effect_or_request(body):
    value = {**receipt(body, 'request'), 'state': 'drained', 'workers': [],
             'counts': {'queued': 4, 'parked': 2, 'unsafe_turns': 0, 'effects': 0, 'requests': 1}}
    with pytest.raises(ValueError):
        control.validate(value, body, 'status')
    value['state'] = 'draining'
    assert control.validate(value, body, 'status')['paused']
    value['counts']['requests'], value['state'] = 0, 'drained'
    assert control.validate(value, body, 'status')['generation'] == 8


@pytest.mark.parametrize('command', ['request', 'status', 'resume'])
def test_transport_runs_installed_native_command_only_after_exact_source_probe(monkeypatch, body, command):
    state = {'ok': True, 'ready': True, 'core': 'pydantic', 'identity': '42@2026-10-15T00:00:00+00:00',
             'policy': 'a' * 64, 'contracts': {'worker': 'b' * 64}}
    service = {'role': 'worker', 'staged': False}
    value = receipt(body, command)
    if command == 'status':
        value.update(state='running', workers=[], counts=dict.fromkeys(control.STATUS_COUNTS, 0))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    monkeypatch.setattr(control.subprocess, 'run', run)
    assert control.execute({'service': service, 'command': command, 'body': body},
                           probe_fn=lambda _: state) == value
    assert calls[0][0] == ['/app/.venv/bin/python', '-m', 'moyai.product_drain', command]
    assert json.loads(calls[0][1]['input']) == body
    assert calls[0][1]['capture_output'] and calls[0][1]['timeout'] == 25
    calls.clear()
    state['identity'] = '99@2026-10-15T00:00:00+00:00'
    with pytest.raises(ValueError):
        control.execute({'service': service, 'command': command, 'body': body}, probe_fn=lambda _: state)
    assert not calls


@pytest.mark.parametrize('command', ['request', 'status', 'resume'])
def test_ssh_transport_classifies_only_status_as_read_only_and_never_retries(monkeypatch, body, command):
    calls = []
    class Transport:
        def __init__(self, key, source, **kwargs): self.source = source
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'status': 'unconfirmed'}
    monkeypatch.setattr('scripts.release.deploy.SSHProbe', Transport)
    service = SimpleNamespace(probe_options=lambda: {'fixture': True})
    with pytest.raises(ReleaseError, match='uncertain'):
        control.SSHNativeControl(Path('/synthetic/key'))(service, command, body)
    assert len(calls) == 1
    assert calls[0][1]['read_only'] is (command == 'status')
    assert calls[0][0][1]['body'] == body


def test_control_cli_failure_never_exposes_child_output(monkeypatch, body, capsys):
    monkeypatch.setattr(control.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=1, stdout='private-value', stderr='private-dsn'))
    state = {'ok': True, 'ready': True, 'core': 'pydantic', 'identity': '42@2026-10-15T00:00:00+00:00',
             'policy': 'a' * 64, 'contracts': {'worker': 'b' * 64}}
    with pytest.raises(ValueError) as error:
        control.execute({'service': {'role': 'worker', 'staged': False}, 'command': 'request', 'body': body},
                        probe_fn=lambda _: state)
    assert 'private' not in str(error.value) and capsys.readouterr() == ('', '')


def test_owner_timezone_normalization_matches_native_receipt_fingerprint(body):
    normalized = deepcopy(body)
    body['expected_worker']['backend_start'] = '2026-10-14T17:00:00-07:00'
    assert control.owner('42@2026-10-14T17:00:00-07:00') == {
        'pid': 42, 'backend_start': '2026-10-15T00:00:00+00:00'}
    assert control.fingerprint(body, paused=True) == control.fingerprint(normalized, paused=True)
    assert control.validate(receipt(normalized, 'request'), body, 'request')['generation'] == 8
    with pytest.raises(ValueError):
        control.owner('42@2026-10-15T00:00:00')
