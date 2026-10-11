"""Fixed, credential-free SSH failure facts; never retain remote output."""
import json


TRANSIENT_KINDS = {'connect_timeout', 'connection_reset', 'connection_refused', 'connection_closed'}
KINDS = TRANSIENT_KINDS | {'auth_denied', 'host_key_rejected', 'session_open_rejected',
                          'command_timeout', 'remote_exit', 'local_os_error', 'unknown'}
OUTPUT_STATES = {'absent', 'complete', 'invalid', 'duplicate', 'other'}


def output_state(stdout, marker):
    if not stdout:
        return 'absent'
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    lines = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
    if len(lines) > 1:
        return 'duplicate'
    if lines:
        try:
            return 'complete' if isinstance(json.loads(lines[0]), dict) else 'invalid'
        except ValueError:
            return 'invalid'
    return 'other'


def failure(*, stdout, stderr, returncode, marker, elapsed_ms, kind=None):
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    stderr = stderr or ''
    if kind is None:
        # Security failures take precedence over an accompanying connection close.
        for category, messages in (
            ('auth_denied', ('Permission denied', 'Too many authentication failures')),
            ('host_key_rejected', ('Host key verification failed', 'REMOTE HOST IDENTIFICATION HAS CHANGED')),
            ('session_open_rejected', ('open failed: administratively prohibited', 'session request failed')),
            ('connect_timeout', ('Connection timed out',)),
            ('connection_reset', ('Connection reset by peer',)),
            ('connection_refused', ('Connection refused',)),
            ('connection_closed', ('Connection closed by',)),
        ):
            if any(message in stderr for message in messages):
                kind = category
                break
        else:
            kind = 'remote_exit' if returncode != 255 else 'unknown'
    output = output_state(stdout, marker)
    reason = 'ssh_failed'
    if output != 'absent':
        # Even a partial observation could contain drift. Never discard it and
        # accept a later healthy sample after a failed transport.
        reason = 'probe_output_interrupted'
    elif kind == 'command_timeout':
        reason = 'ssh_timeout'
    elif returncode == 255 and kind in TRANSIENT_KINDS:
        reason = 'ssh_connection_lost'
    return {'ok': False, 'reason': reason, 'transport': {
        'kind': kind, 'returncode': returncode, 'elapsed_ms': elapsed_ms, 'output': output,
        'authenticated': 'Authenticated to ' in stderr,
        'command_requested': 'Sending command: ' in stderr,
    }}


def valid_diagnostics(value, reason):
    if not (isinstance(value, dict) and set(value) == {
            'kind', 'returncode', 'elapsed_ms', 'output', 'authenticated', 'command_requested'}
            and isinstance(value['kind'], str) and value['kind'] in KINDS
            and (value['returncode'] is None or (type(value['returncode']) is int
                 and -255 <= value['returncode'] <= 255 and value['returncode'] != 0))
            and type(value['elapsed_ms']) is int and 0 <= value['elapsed_ms'] <= 3_600_000
            and isinstance(value['output'], str) and value['output'] in OUTPUT_STATES
            and type(value['authenticated']) is bool and type(value['command_requested']) is bool):
        return False
    if reason == 'probe_output_interrupted':
        return value['output'] != 'absent'
    if value['output'] != 'absent':
        return False
    if reason == 'ssh_connection_lost':
        return value['returncode'] == 255 and value['kind'] in TRANSIENT_KINDS
    if reason == 'ssh_timeout':
        return value['returncode'] is None and value['kind'] == 'command_timeout'
    return reason == 'ssh_failed'
