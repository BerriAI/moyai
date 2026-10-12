"""Owned SSH observations: fresh commands, fail-closed clients, bounded recovery."""
import json
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

import pytest

from scripts.release import ssh_connections as transport
from scripts.release.deploy import Service, SSHProbe, WORKER
from scripts.release.rehearse import OLD, Render
from scripts.release.ssh_diagnostics import valid_diagnostics


ARGS = ['ssh', '-F', '/dev/null', '-v', '-i', '/private-key',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
        'synthetic@host', 'python - private-options']
OK = SimpleNamespace(returncode=0, stdout='MOYAI_RELEASE_PROBE={"ok":true}', stderr='')


@pytest.fixture
def setup(monkeypatch):
    created, calls, outcomes, starts, elapsed = [], [], [], [], [0]

    class FakeMaster:
        def __init__(self, args, path):
            self.args, self.path = args, path
            self.code, self.diagnostic = starts.pop(0) if starts else (None, b'Authenticated to private-host')
            self.process = SimpleNamespace(poll=lambda: self.code)
            self.reader = SimpleNamespace(join=lambda **kw: None)
            self.closed = False
            if self.code is None:
                path.touch()
            created.append(self)

        def diagnostics(self):
            return self.diagnostic

        def close(self):
            self.closed, self.code = True, -15
            self.path.unlink(missing_ok=True)
            return True

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if '-O' in args:
            return OK
        value = outcomes.pop(0) if outcomes else OK
        if isinstance(value, BaseException):
            raise value
        return value

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(transport, 'Master', FakeMaster)
    monkeypatch.setattr(transport.subprocess, 'run', run)
    with transport.SSHConnections(clock=lambda: elapsed[0], sleep=sleep) as pool:
        yield SimpleNamespace(pool=pool, created=created, calls=calls, outcomes=outcomes,
            starts=starts, elapsed=elapsed, service=SimpleNamespace(id='worker', deploy_id='old'))


def run(setup, service=None, *, timeout=50):
    return setup.pool.run(service or setup.service, ARGS, input='fresh source', timeout=timeout)


def command_calls(setup):
    return [args for args, kw in setup.calls if '-O' not in args]


def test_one_owned_master_per_service_and_deployment_runs_each_command_fresh(setup):
    run(setup)
    run(setup)
    broker = SimpleNamespace(id='broker', deploy_id='broker-old')
    run(setup, broker)
    assert len(setup.created) == 2 and len(command_calls(setup)) == 3
    first, unaffected = setup.created
    assert '-M' in first.args and '-N' in first.args and '-f' not in first.args
    assert first.args[-1] == 'synthetic@host' and 'private-options' not in ' '.join(first.args)
    setup.service.deploy_id = 'new'
    run(setup)
    assert first.closed and not unaffected.closed and len(setup.created) == 3
    assert len(str(setup.created[-1].path).encode()) < 104
    assert Path(setup.pool.directory.name).stat().st_mode & 0o777 == 0o700
    for args, kwargs in setup.calls:
        assert 'ProxyCommand=/bin/false' in args
        assert 'ControlMaster=no' in args and 'ControlPersist=no' in args
        assert 'StrictHostKeyChecking=yes' in args and 'IdentitiesOnly=yes' in args
        if '-O' not in args:
            assert kwargs['input'] == 'fresh source'


@pytest.mark.parametrize('outcome,reason', [
    (SimpleNamespace(returncode=255, stdout='', stderr='mux_client_read_packet: read header failed: Broken pipe'), 'ssh_connection_lost'),
    (SimpleNamespace(returncode=255, stdout='', stderr='session request failed'), 'ssh_failed'),
    (SimpleNamespace(returncode=255, stdout='MOYAI_RELEASE_PROBE={"ok":false}', stderr='Connection reset by peer'), 'probe_output_interrupted'),
    (subprocess.TimeoutExpired('ssh', 50, output=b'MOYAI_RELEASE_PROBE={"ok":', stderr=b'private-timeout'), 'probe_output_interrupted'),
    (subprocess.TimeoutExpired('ssh', 50, stderr=b'private-timeout'), 'ssh_timeout'),
    (OSError('private-os-error'), 'ssh_failed'),
])
def test_every_failed_command_is_single_shot_sanitized_and_invalidates_master(setup, outcome, reason):
    setup.outcomes.append(outcome)
    with pytest.raises(transport.ConnectionFailure) as caught:
        run(setup)
    error = caught.value
    assert len(command_calls(setup)) == 1 and setup.created[0].closed and not setup.pool.masters
    assert error.result['reason'] == reason
    assert valid_diagnostics(error.result['transport'], reason)
    assert 'private-' not in json.dumps(error.result) and 'private-' not in str(error)
    assert error.result['transport']['authenticated'] is True
    run(setup)
    assert len(setup.created) == 2 and len(command_calls(setup)) == 2


@pytest.mark.parametrize('marker,kind', [(b'Permission denied', 'auth_denied'),
                                       (b'Host key verification failed', 'host_key_rejected')])
@pytest.mark.parametrize('timeout', [False, True])
def test_master_security_error_precedes_generic_mux_failure_or_timeout(setup, marker, kind, timeout):
    run(setup)
    setup.created[0].diagnostic += marker
    setup.outcomes.append(subprocess.TimeoutExpired('ssh', 50) if timeout else
        SimpleNamespace(returncode=255, stdout='', stderr='Connection closed by remote host'))
    with pytest.raises(transport.ConnectionFailure) as caught:
        run(setup)
    assert caught.value.result['reason'] == 'ssh_failed'
    assert caught.value.result['transport']['kind'] == kind
    assert setup.pool.next_connection == 0 and setup.created[0].closed


def test_preauth_cooldown_is_bounded_and_only_connects_once_per_observation(setup):
    for expected_time in (0, 10, 30):
        setup.starts.append((255, b'Connection reset by peer private-host'))
        with pytest.raises(transport.ConnectionFailure) as caught:
            run(setup)
        assert caught.value.result['reason'] == 'ssh_connection_lost'
        assert setup.elapsed[0] == expected_time
    assert len(setup.created) == 3 and not command_calls(setup)
    run(setup)
    assert setup.elapsed[0] == 50 and setup.pool.pre_auth_failures == 0
    assert len(command_calls(setup)) == 1


@pytest.mark.parametrize('stderr', [b'Authenticated to host\nConnection reset by peer',
                                    b'Permission denied', b'Host key verification failed', b'unknown'])
def test_authenticated_or_unclassified_failures_never_schedule_preauth_cooldown(setup, stderr):
    setup.starts.append((255, stderr))
    with pytest.raises(transport.ConnectionFailure):
        run(setup)
    assert setup.pool.next_connection == 0


def test_cooldown_uses_original_timeout_budget_and_cannot_start_after_deadline(setup):
    setup.pool.next_connection = 20
    with pytest.raises(transport.ConnectionFailure) as caught:
        run(setup, timeout=5)
    assert caught.value.result['reason'] == 'ssh_timeout'
    assert setup.elapsed[0] == 5 and not setup.created and not setup.calls


def test_connection_check_and_command_share_timeout_budget(setup, monkeypatch):
    original = transport.subprocess.run
    def check(args, **kwargs):
        if '-O' in args:
            setup.elapsed[0] += 1
        return original(args, **kwargs)
    monkeypatch.setattr(transport.subprocess, 'run', check)
    run(setup, timeout=5)
    assert setup.calls[-1][1]['timeout'] == 4


@pytest.mark.parametrize('stage', ['setup', 'command'])
def test_cancellation_cleans_master_without_replaying_command(setup, monkeypatch, stage):
    original = transport.subprocess.run
    def cancel(args, **kwargs):
        if ('-O' in args) == (stage == 'setup'):
            raise KeyboardInterrupt()
        return original(args, **kwargs)
    monkeypatch.setattr(transport.subprocess, 'run', cancel)
    with pytest.raises(KeyboardInterrupt):
        run(setup)
    assert setup.created[0].closed and not setup.pool.masters


def test_sigint_during_spawn_is_delivered_after_child_is_owned(setup, monkeypatch):
    original = transport.Master
    def interrupted(*args):
        master = original(*args)
        signal.raise_signal(signal.SIGINT)
        return master
    monkeypatch.setattr(transport, 'Master', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(setup)
    assert setup.created[0].closed and not setup.pool.masters and not setup.calls


@pytest.mark.parametrize('stdout', ['', 'MOYAI_RELEASE_PROBE=[]', 'MOYAI_RELEASE_PROBE={partial',
                                   'MOYAI_RELEASE_PROBE={}\nMOYAI_RELEASE_PROBE={}'])
def test_malformed_success_is_terminal_and_closes_the_observation_connection(setup, stdout):
    env = Render().env[WORKER]
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-original')
    setup.outcomes.append(SimpleNamespace(returncode=0, stdout=stdout, stderr=''))
    result = SSHProbe(Path('/synthetic-key'), connections=setup.pool)(service)
    assert result == {'ok': False, 'reason': 'probe_output_invalid'}
    assert len(command_calls(setup)) == 1 and setup.created[0].closed


def test_write_command_bypasses_pool_even_when_probe_has_a_connection_pool(setup):
    env = Render().env[WORKER]
    service = Service(WORKER, 'worker', env, dict(env), OLD, 'dep-original')
    result = SSHProbe(Path('/synthetic-key'), connections=setup.pool).run(
        service, {}, marker='MOYAI_RELEASE_PROBE=', source='synthetic migration')
    assert result == {'ok': True} and not setup.created and len(setup.calls) == 1
    assert 'ControlMaster=no' in setup.calls[0][0] and 'ControlPath=none' in setup.calls[0][0]


@pytest.mark.parametrize('accepted', [False, True])
def test_prior_master_commands_do_not_claim_current_mux_session_was_requested(setup, accepted):
    run(setup)
    setup.created[0].diagnostic += b'\ndebug1: Sending command: previous-private-command\n'
    stderr = 'Connection closed by remote host'
    if accepted:
        stderr += '\ndebug1: mux_client_request_session: master session id: 2'
    setup.outcomes.append(SimpleNamespace(returncode=255, stdout='', stderr=stderr))
    with pytest.raises(transport.ConnectionFailure) as caught:
        run(setup)
    assert caught.value.result['transport']['command_requested'] is accepted
    assert caught.value.result['transport']['authenticated'] is True
