"""Bounded transport for the native runtime's maintenance protocol.

This selects no execution implementation and creates no runtime owner. Requests
and resumes are single-shot writes; status is an explicitly read-only operation.
"""
from datetime import UTC, datetime
import hashlib
import json
import re
import subprocess
import sys

RECEIPT_KEYS = {
    'protocol', 'core', 'native_schema', 'task_queue', 'generation', 'paused',
    'request_key', 'request_fingerprint', 'requester', 'source_policy',
}
STATUS_COUNTS = {'queued', 'parked', 'unsafe_turns', 'effects', 'requests'}


def require(condition):
    if not condition:
        raise ValueError('Native maintenance receipt is incomplete or mismatched.')


def owner(identity):
    pid, started = identity.split('@', 1)
    require(pid.isdigit() and int(pid) > 0)
    return {'pid': int(pid), 'backend_start': utc_start(started)}


def utc_start(value):
    require(isinstance(value, str))
    started = datetime.fromisoformat(value)
    require(started.tzinfo is not None)
    return started.astimezone(UTC).isoformat()


def fingerprint(body, *, paused):
    worker = body['expected_worker']
    return hashlib.sha256(json.dumps({
        'paused': paused, 'expected_generation': body['expected_generation'],
        'request_key': body['request_key'], 'owner_pid': worker['pid'],
        'owner_started': utc_start(worker['backend_start']),
        'source_policy': worker['source_policy'], 'worker_policy': worker['worker_policy'],
        'native_schema': body['native_schema'], 'task_queue': body['task_queue'],
    }, sort_keys=True).encode()).hexdigest()


def validate(value, body, command):
    require(isinstance(value, dict) and set(value) == (
        RECEIPT_KEYS | {'counts', 'state', 'workers'} if command == 'status' else RECEIPT_KEYS))
    require(value['protocol'] == 1 and type(value['protocol']) is int
            and value['core'] == 'pydantic-v1'
            and all(value[key] == body[key] for key in ('native_schema', 'task_queue'))
            and type(value['generation']) is int and value['generation'] >= 0
            and type(value['paused']) is bool
            and isinstance(value['request_key'], str) and 0 < len(value['request_key']) <= 200
            and isinstance(value['request_fingerprint'], str)
            and re.fullmatch('[0-9a-f]{64}', value['request_fingerprint']))
    require(isinstance(value['requester'], dict) and set(value['requester']) == {'pid', 'backend_start'})
    if value['generation'] == 0:
        require(value['paused'] and value['request_key'] == 'bootstrap'
                and value['requester'] == {'pid': None, 'backend_start': None}
                and value['source_policy'] is None
                and value['request_fingerprint'] == hashlib.sha256(b'bootstrap').hexdigest())
    else:
        requester = value['requester']
        require(type(requester['pid']) is int and requester['pid'] > 0
                and isinstance(requester['backend_start'], str)
                and datetime.fromisoformat(requester['backend_start']).tzinfo is not None
                and isinstance(value['source_policy'], str) and re.fullmatch('[0-9a-f]{64}', value['source_policy']))
    if command != 'status':
        expected = body['expected_worker']
        require(value['generation'] == body['expected_generation'] + 1
                and value['paused'] is (command == 'request')
                and value['request_key'] == body['request_key']
                and value['requester'] == {'pid': expected['pid'], 'backend_start': utc_start(expected['backend_start'])}
                and value['source_policy'] == expected['source_policy']
                and value['request_fingerprint'] == fingerprint(body, paused=command == 'request'))
    else:
        require(isinstance(value['counts'], dict) and set(value['counts']) == STATUS_COUNTS
                and all(type(count) is int and count >= 0 for count in value['counts'].values()))
        expected_state = ('running' if not value['paused'] else 'draining' if any(
            value['counts'][key] for key in ('unsafe_turns', 'effects', 'requests')) else 'drained')
        require(value['state'] == expected_state and isinstance(value['workers'], list))
        identities = []
        for worker in value['workers']:
            require(isinstance(worker, dict) and set(worker) == {
                'pid', 'backend_start', 'maintenance_drain', 'source_policy', 'worker_policy'})
            require(type(worker['pid']) is int and worker['pid'] > 0
                    and type(worker['maintenance_drain']) is bool
                    and isinstance(worker['backend_start'], str)
                    and datetime.fromisoformat(worker['backend_start']).tzinfo is not None
                    and all(isinstance(worker[key], str) and re.fullmatch('[0-9a-f]{64}', worker[key])
                            for key in ('source_policy', 'worker_policy')))
            identities.append((worker['pid'], utc_start(worker['backend_start'])))
        require(len(identities) == len(set(identities)))
    return {key: value[key] for key in RECEIPT_KEYS}


def execute(options, *, probe_fn):
    command, body, service = options['command'], options['body'], options['service']
    require(command in {'request', 'status', 'resume'})
    state = probe_fn(service)
    require(state.get('ok') is True and state.get('ready') is True and state.get('core') == 'pydantic')
    expected = body.get('expected_worker')
    if expected is not None:
        require(service['role'] == 'worker' and not service['staged']
                and owner(state['identity']) == {'pid': expected['pid'], 'backend_start': utc_start(expected['backend_start'])}
                and state['policy'] == expected['source_policy']
                and state['contracts']['worker'] == expected['worker_policy'])
    else:
        require(command == 'status' and service['staged'] and state['identity'] is None
                and all(state[key] == [] for key in ('owners', 'coordinators', 'brokers', 'broker_leaders')))
    encoded = json.dumps(body)
    require(len(encoded.encode()) <= 16384)
    from pathlib import Path
    previous = Path('/app/native/.venv/bin/python')
    executable = str(previous) if previous.is_file() else '/app/.venv/bin/python'
    result = subprocess.run([executable, '-m', 'moyai.product_drain', command],
        input=encoded, cwd='/app', capture_output=True, text=True, timeout=25)
    require(result.returncode == 0 and len(result.stdout.encode()) <= 16384)
    receipt = json.loads(result.stdout)
    validate(receipt, body, command)
    return receipt


class SSHNativeControl:
    def __init__(self, key, *, connections=None):
        from scripts.release.deploy import ROOT, SSHProbe
        self.transport = SSHProbe(key, source=ROOT / 'native_control.py', connections=connections)

    def __call__(self, service, command, body):
        from scripts.release.deploy import ReleaseError
        from scripts.release.maintenance_probe import piped_source
        source = ("_probe = {'__name__': 'native_control_probe'}\n"
                  'exec(' + repr(piped_source()) + ', _probe)\n' + self.transport.source.read_text())
        value = self.transport.run(service,
            {'service': service.probe_options(), 'command': command, 'body': body},
            marker='MOYAI_NATIVE_CONTROL=', timeout=50, source=source, read_only=command == 'status')
        try:
            validate(value, body, command)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ReleaseError('Native maintenance command failed or its outcome is uncertain; inspect the recorded intent and generation before recovery.') from None
        return value


if __name__ == '__main__':
    try:
        value = execute(json.loads(sys.argv[1]), probe_fn=_probe['probe'])
    except Exception:
        value = {'status': 'unconfirmed'}
    print('MOYAI_NATIVE_CONTROL=' + json.dumps(value, sort_keys=True))
