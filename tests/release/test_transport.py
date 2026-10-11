"""Secret-safe API transport and authenticated read-only SSH invocation."""
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

from scripts.release.deploy import API, NoRedirect, ReleaseError, Service, SSHProbe, WORKER
from scripts.release.rehearse import OLD, Render
from scripts.release.ssh_diagnostics import valid_diagnostics


def test_failed_http_write_is_never_retried_or_logged():
    api = API('https://api.render.com/v1', 'synthetic-secret')
    calls = []
    def failed(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 503, 'synthetic-secret', {}, io.BytesIO(b'secret-body'))
    api.opener.open = failed
    with pytest.raises(ReleaseError) as error:
        api.call('POST', '/services/synthetic/deploys', {'commitId': OLD})
    assert len(calls) == 1
    assert 'synthetic-secret' not in str(error.value)
    assert 'secret-body' not in str(error.value)
    assert 'may have been accepted' in str(error.value)


def test_authenticated_requests_cannot_follow_redirects():
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://different-host') is None


def test_render_inventory_follows_cursor_and_rejects_nonadvancing_pages():
    api = API('https://api.render.com/v1', 'synthetic-secret')
    paths = []
    first = [{'cursor': str(i), 'envVar': {'key': str(i), 'value': 'safe'}} for i in range(100)]
    def page(method, path):
        paths.append(path)
        return first if 'cursor' not in path else [{'cursor': 'done', 'envVar': {'key': 'last', 'value': 'safe'}}]
    api.call = page
    assert len(api.pages('/services/synthetic/env-vars', 'envVar')) == 101
    assert 'cursor=99' in paths[-1]
    api.call = lambda *args: first
    with pytest.raises(ReleaseError, match='incomplete inventory'):
        api.pages('/services/synthetic/env-vars', 'envVar')


def test_ssh_uses_pinned_host_and_only_sends_read_only_probe(monkeypatch):
    env = Render().env[WORKER]
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-synthetic')
    def subprocess_run(args, *, input, text, capture_output, timeout):
        assert 'StrictHostKeyChecking=yes' in args
        assert '-v' in args
        assert 'IdentitiesOnly=yes' in args
        assert 'GlobalKnownHostsFile=/dev/null' in args
        assert WORKER + '@ssh.oregon.render.com' in args
        assert 'runuser -u workspace' in args[-1]
        assert all(value not in args[-1] for value in ('do-not-log-session', 'do-not-log-encryption', 'postgresql://'))
        assert input == Path('scripts/release/probe.py').read_text()
        assert timeout == 50 and capture_output
        return SimpleNamespace(returncode=0, stdout='MOYAI_RELEASE_PROBE=' + json.dumps({'ok': True}))
    monkeypatch.setattr('scripts.release.deploy.subprocess.run', subprocess_run)
    assert SSHProbe(Path('/synthetic/key'))(service) == {'ok': True}


@pytest.mark.parametrize('outcome,reason', [
    ('timeout', 'ssh_timeout'), ('reset', 'ssh_connection_lost'),
    ('refused', 'ssh_connection_lost'), ('closed', 'ssh_connection_lost'),
    ('auth', 'ssh_failed'), ('host_key', 'ssh_failed'), ('unknown', 'ssh_failed'),
    ('malformed', 'probe_output_invalid'), ('missing', 'probe_output_invalid'),
    ('duplicate', 'probe_output_invalid'), ('array', 'probe_output_invalid'),
    ('null', 'probe_output_invalid'), ('os_error', 'ssh_failed'),
])
def test_ssh_failure_codes_are_safe_and_transport_remains_single_shot(monkeypatch, capsys, outcome, reason):
    env, calls = Render().env[WORKER], []
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-synthetic')
    def run(*args, **kwargs):
        calls.append(args)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('ssh', 50, stderr='private-error')
        if outcome == 'os_error':
            raise OSError('private-error')
        errors = {'reset': 'Connection reset by peer', 'refused': 'Connection refused',
                  'closed': 'Connection closed by remote host', 'auth': 'Permission denied\nConnection closed by host',
                  'host_key': 'Host key verification failed\nConnection closed by host', 'unknown': 'unknown exit'}
        output = '' if outcome in errors else 'MOYAI_RELEASE_PROBE={"ok":true}'
        if outcome == 'malformed': output = 'MOYAI_RELEASE_PROBE={private-output'
        elif outcome == 'missing': output = 'private-output'
        elif outcome == 'duplicate': output += '\n' + output
        elif outcome == 'array': output = 'MOYAI_RELEASE_PROBE=[]'
        elif outcome == 'null': output = 'MOYAI_RELEASE_PROBE=null'
        return SimpleNamespace(returncode=255 if outcome in errors else 0, stdout=output,
                               stderr=errors.get(outcome, '') + ' private-error')
    monkeypatch.setattr(subprocess, 'run', run)
    result = SSHProbe(Path('/synthetic/key'))(service)
    assert result['ok'] is False and result['reason'] == reason and len(calls) == 1
    if reason.startswith('ssh_'):
        assert valid_diagnostics(result['transport'], reason)
    else:
        assert result == {'ok': False, 'reason': reason}
    assert 'private-' not in json.dumps(result) and capsys.readouterr() == ('', '')


@pytest.mark.parametrize('stderr,code,kind,reason', [
    ('Connection timed out', 255, 'connect_timeout', 'ssh_connection_lost'),
    ('Connection reset by peer', 255, 'connection_reset', 'ssh_connection_lost'),
    ('Connection refused', 255, 'connection_refused', 'ssh_connection_lost'),
    ('Connection closed by remote host', 255, 'connection_closed', 'ssh_connection_lost'),
    ('Permission denied\nConnection closed by host', 255, 'auth_denied', 'ssh_failed'),
    ('Too many authentication failures', 255, 'auth_denied', 'ssh_failed'),
    ('Host key verification failed\nConnection reset by peer', 255, 'host_key_rejected', 'ssh_failed'),
    ('REMOTE HOST IDENTIFICATION HAS CHANGED', 255, 'host_key_rejected', 'ssh_failed'),
    ('channel 0: open failed: administratively prohibited', 255, 'session_open_rejected', 'ssh_failed'),
    ('mux_client_request_session: session request failed', 255, 'session_open_rejected', 'ssh_failed'),
    ('Connection reset by peer', 1, 'connection_reset', 'ssh_failed'),
    ('private failure', 1, 'remote_exit', 'ssh_failed'),
    ('private failure', -9, 'remote_exit', 'ssh_failed'),
    ('private failure', 255, 'unknown', 'ssh_failed'),
])
def test_failure_receipts_classify_transport_without_exposing_verbose_output(monkeypatch, stderr, code, kind, reason):
    env = Render().env[WORKER]
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-synthetic')
    stderr = ('debug1: Authenticated to private-host using publickey.\n'
              'debug1: Sending command: private-command-with-secrets\n' + stderr)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=code, stdout='', stderr=stderr))
    result = SSHProbe(Path('/private-key-path'))(service)
    assert result['reason'] == reason
    assert result['transport']['kind'] == kind and result['transport']['returncode'] == code
    assert result['transport']['authenticated'] is True
    assert result['transport']['command_requested'] is True
    assert result['transport']['output'] == 'absent'
    assert valid_diagnostics(result['transport'], reason)
    assert 'private-' not in json.dumps(result)


@pytest.mark.parametrize('output,state', [
    ('MOYAI_RELEASE_PROBE={"ok":true,"ready":false,"policy":"private-drift"}', 'complete'),
    ('MOYAI_RELEASE_PROBE={"ok":false,"reason":"runtime_validation_failed"}', 'complete'),
    ('MOYAI_RELEASE_PROBE={"ok":', 'invalid'),
    ('MOYAI_RELEASE_PROBE={}\nMOYAI_RELEASE_PROBE={}', 'duplicate'),
    ('MOYAI_RELEASE_PRO', 'other'),
    ('private-output', 'other'),
])
@pytest.mark.parametrize('timeout', [False, True])
def test_interrupted_observations_are_never_retryable(monkeypatch, output, state, timeout):
    env, calls = Render().env[WORKER], []
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-synthetic')
    def run(*args, **kwargs):
        calls.append(args)
        if timeout:
            raise subprocess.TimeoutExpired('ssh', 50, output=output.encode(), stderr=b'private-error')
        return SimpleNamespace(returncode=255, stdout=output, stderr='Connection reset by peer private-error')
    monkeypatch.setattr(subprocess, 'run', run)
    result = SSHProbe(Path('/synthetic-key'))(service)
    assert len(calls) == 1 and result['reason'] == 'probe_output_interrupted'
    assert result['transport']['output'] == state
    assert valid_diagnostics(result['transport'], result['reason'])
    assert 'private-' not in json.dumps(result)
