"""Real local subprocesses, with no sandbox provider or model requests."""
import json
import os
import signal
import sys
import time

import pytest

from sandbox import agent
from sandbox.durable_process import DeadlineExpired, execution_timeout, status, supervise


def test_recomputes_stale_relative_timeout_and_preserves_unbounded_runs():
    assert execution_timeout({'deadline_at': 110, 'timeout': 60}, now=108) == 2
    assert execution_timeout({'deadline_at': 110, 'timeout': 1}, now=108) == 1
    assert execution_timeout({'timeout': 60}, now=108) == 60
    assert execution_timeout({}, now=108) is None
    with pytest.raises(DeadlineExpired):
        execution_timeout({'deadline_at': 110, 'timeout': 60}, now=111)
    for invalid in (float('nan'), float('inf'), True, '110'):
        with pytest.raises(ValueError):
            execution_timeout({'deadline_at': invalid})


def test_expired_supervisor_never_launches_and_retains_launch_once_receipt(tmp_path):
    marker = tmp_path / 'executed'
    command = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
    supervise(tmp_path / 'journal', command, deadline_at=time.time() - 1)
    supervise(tmp_path / 'journal', command, deadline_at=time.time() + 60)
    assert not marker.exists()
    receipt = status(tmp_path / 'journal')
    assert receipt['state'] == 'done' and receipt['exit_code'] == 124
    assert receipt['final']['completed'] is False


def test_deadline_kills_silent_agent_and_native_child_without_a_host_worker(tmp_path):
    marker = tmp_path / 'native-child-finished'
    launched = tmp_path / 'native-child-started'
    native = f'import time; from pathlib import Path; time.sleep(5); Path({str(marker)!r}).touch()'
    program = ('import signal, subprocess, sys, time\nfrom pathlib import Path\n'
               'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
               f'subprocess.Popen([sys.executable, "-c", {native!r}])\n'
               f'Path({str(launched)!r}).touch()\n'
               'time.sleep(20)\n')
    started = time.monotonic()
    supervise(tmp_path / 'journal', [sys.executable, '-c', program], deadline_at=time.time() + 0.6)
    assert time.monotonic() - started < 3
    assert launched.exists()
    assert not marker.exists()
    receipt = status(tmp_path / 'journal')
    assert receipt['exit_code'] == 124
    assert any(event['data'].get('phase') == 'deadline' for event in receipt['events'] if 'data' in event)


def test_completed_process_before_deadline_retains_final_answer(tmp_path):
    event = {'kind': 'final', 'message': 'Public result', 'completed': True}
    program = f'print({("WORKSPACE_EVENT " + json.dumps(event))!r}, flush=True)'
    supervise(tmp_path / 'journal', [sys.executable, '-c', program], deadline_at=time.time() + 10)
    receipt = status(tmp_path / 'journal')
    assert receipt['exit_code'] == 0
    assert receipt['final'] == event


def test_deadline_kills_native_descendant_after_parent_and_stdout_have_closed(tmp_path):
    marker = tmp_path / 'escaped-deadline'
    pid_file = tmp_path / 'native-pid'
    native = ('import os, signal, time\nfrom pathlib import Path\n'
              'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
              f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
              'time.sleep(1.4)\n'
              f'Path({str(marker)!r}).touch()\n'
              'time.sleep(10)\n')
    program = ('import subprocess, sys, time\n'
               f'subprocess.Popen([sys.executable, "-c", {native!r}], '
               'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
               'time.sleep(20)\n')
    try:
        supervise(tmp_path / 'journal', [sys.executable, '-c', program], deadline_at=time.time() + 0.6)
        assert pid_file.exists()
        assert status(tmp_path / 'journal')['exit_code'] == 124
        time.sleep(1)
        assert not marker.exists(), 'Native child outlived the deadline after its parent exited'
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_expired_agent_does_not_start_broker_or_native_runtime(monkeypatch):
    events = []
    monkeypatch.setattr(agent, 'BrokerRelay', lambda *a, **k: pytest.fail('Expired work must not connect'))
    monkeypatch.setattr(agent, 'emit', lambda *a, **k: events.append((a, k)))
    assert agent.run({'deadline_at': time.time() - 1, 'timeout': 100}) == 124
    assert events[0][1] == {'completed': False}
