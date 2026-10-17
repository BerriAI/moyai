"""Real local process/socket tests; instrumented worker, not Chromium E2E proof."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "browser_driver.py"
spec = importlib.util.spec_from_file_location("browser_driver", SOURCE)
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)

WORKER = """
import json,os,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1])
for line in sys.stdin:
    body=json.loads(line)
    action=body['command']['action']
    with (root/'dispatches').open('a') as log:
        log.write(action+'\\n')
    if action=='hang':
        subprocess.Popen([sys.executable,'-c', "import time;from pathlib import Path;time.sleep(1);Path("+repr(str(root/'late'))+").write_text('late')"])
        time.sleep(60)
    if action=='lost-reply':
        os._exit(1)
    if action=='error':
        print(json.dumps({'error':True}),flush=True)
    else:
        print(json.dumps({'result':{'pid':os.getpid(),'action':action}}),flush=True)
"""


@pytest.fixture
def service():
    # AF_UNIX paths must also fit macOS's shorter sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="mb-", dir="/tmp") as directory:
        root = Path(directory)
        worker = root / "worker.py"
        worker.write_text(WORKER.replace("root=Path(sys.argv[1])", f"root=Path({str(root)!r})"))
        processes = []

        def start(idle=10):
            state = root / str(len(processes))
            state.mkdir()
            code = (
                f"import sys;sys.path.insert(0,{str(SOURCE.parent)!r});"
                f"from browser_driver import serve;from pathlib import Path;"
                f"serve(Path({str(state)!r}),({sys.executable!r},{str(worker)!r},{str(root)!r}),idle_seconds={idle!r})"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", code], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            processes.append(process)
            for _ in range(200):
                if (state / driver.SOCKET_NAME).exists():
                    return state, process
                assert process.poll() is None
                time.sleep(.01)
            raise AssertionError("Supervisor did not bind its socket.")

        yield root, start
        for process in processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def request(state, action, seconds=3):
    deadline = time.time() + seconds
    frame = json.dumps({"command": {"action": action}, "deadline": deadline}).encode() + b"\n"
    return driver.exchange(state, frame, deadline)


def test_warm_requests_reuse_one_worker_and_private_socket(service):
    _, start = service
    state, process = start()
    first = request(state, "first")
    second = request(state, "second")
    assert first["pid"] == second["pid"]
    assert first["pid"] != process.pid
    assert (state / driver.SOCKET_NAME).stat().st_mode & 0o777 == 0o600


def test_deadline_kills_worker_descendants_and_next_service_recovers(service):
    root, start = service
    state, process = start()
    assert request(state, "warmup")["action"] == "warmup"
    outside = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    try:
        began = time.monotonic()
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            request(state, "hang", seconds=.3)
        process.wait(timeout=2)
        assert time.monotonic() - began < 2
        assert outside.poll() is None
        time.sleep(1.2)
        assert not (root / "late").exists()
        next_state, _ = start()
        assert request(next_state, "recovered")["action"] == "recovered"
        assert (root / "dispatches").read_text().splitlines() == ["warmup", "hang", "recovered"]
    finally:
        os.killpg(outside.pid, signal.SIGKILL)
        outside.wait(timeout=2)


def test_disconnected_caller_retires_worker_before_its_long_deadline(service):
    root, start = service
    state, process = start()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(state / driver.SOCKET_NAME))
    connection.sendall(json.dumps({"command": {"action": "hang"}, "deadline": time.time()+30}).encode()+b"\n")
    for _ in range(200):
        if (root / "dispatches").exists() and (root / "dispatches").read_text() == "hang\n":
            break
        time.sleep(.01)
    assert (root / "dispatches").read_text() == "hang\n"
    connection.close()
    process.wait(timeout=2)
    time.sleep(1.2)
    assert not (root / "late").exists()


def test_lost_reply_is_not_classified_as_safe_to_retry(service):
    root, start = service
    state, process = start()
    with pytest.raises(ConnectionError) as error:
        request(state, "lost-reply")
    assert not isinstance(error.value, driver.DriverUnavailable)
    process.wait(timeout=2)
    assert (root / "dispatches").read_text() == "lost-reply\n"


def test_worker_error_retires_its_group_and_preserves_error(service):
    _, start = service
    state, process = start()
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        request(state, "error")
    process.wait(timeout=2)
    state, _ = start()
    assert request(state, "recovered")["action"] == "recovered"


def test_expired_request_never_reaches_worker(service):
    root, start = service
    state, process = start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(state / driver.SOCKET_NAME))
        client.sendall(json.dumps({"command": {"action": "expired"}, "deadline": time.time()-1}).encode()+b"\n")
        client.settimeout(2)
        assert client.recv(100) == b""
    process.wait(timeout=2)
    assert not (root / "dispatches").exists()


def test_idle_supervisor_terminates_its_worker(service):
    _, start = service
    state, process = start(idle=.3)
    worker = request(state, "first")["pid"]
    process.wait(timeout=2)
    # kill(pid, 0) can observe a zombie until init reaps it; ps must show no live worker.
    result = subprocess.run(["ps", "-p", str(worker), "-o", "stat="], check=False, capture_output=True, text=True)
    assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


def stop_managed(state):
    if not (state / driver.PID_NAME).exists():
        return
    pid = int((state / driver.PID_NAME).read_text())
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="The guest process manager runs on Linux.")
def test_managed_call_reuses_worker_then_recovers_without_replaying_timeout(service):
    root, _ = service
    state, helper = root / "managed", root / "worker.py"
    try:
        first = driver.call(state, helper, {"action": "first"}, time.time()+5, os.environ)
        assert driver.call(state, helper, {"action": "second"}, time.time()+5, os.environ)["pid"] == first["pid"]
        previous = int((state / driver.PID_NAME).read_text())
        with pytest.raises((ConnectionError, TimeoutError, OSError)):
            driver.call(state, helper, {"action": "hang"}, time.time()+.3, os.environ)
        result = driver.call(state, helper, {"action": "recovered"}, time.time()+5, os.environ)
        assert result["pid"] != first["pid"]
        assert int((state / driver.PID_NAME).read_text()) != previous
        assert (root / "dispatches").read_text().splitlines() == ["first", "second", "hang", "recovered"]
        time.sleep(1.2)
        assert not (root / "late").exists()
    finally:
        stop_managed(state)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="The guest process manager runs on Linux.")
def test_helper_version_change_replaces_the_owned_process_group(service):
    root, _ = service
    state, first_helper, next_helper = root / "managed", root / "worker.py", root / "next-worker.py"
    next_helper.write_text(first_helper.read_text())
    try:
        first = driver.call(state, first_helper, {"action": "first"}, time.time()+5, os.environ)
        previous = int((state / driver.PID_NAME).read_text())
        result = driver.call(state, next_helper, {"action": "second"}, time.time()+5, os.environ)
        assert result["pid"] != first["pid"]
        assert int((state / driver.PID_NAME).read_text()) != previous
    finally:
        stop_managed(state)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="The guest process manager runs on Linux.")
def test_stale_pid_file_does_not_kill_an_unrelated_process(service):
    root, _ = service
    state, helper = root / "managed", root / "worker.py"
    state.mkdir()
    outside = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True)
    (state / driver.PID_NAME).write_text(str(outside.pid))
    try:
        assert driver.call(state, helper, {"action": "first"}, time.time()+5, os.environ)["action"] == "first"
        assert outside.poll() is None
    finally:
        stop_managed(state)
        os.killpg(outside.pid, signal.SIGKILL)
        outside.wait(timeout=2)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="The guest process manager runs on Linux.")
def test_client_retires_orphaned_worker_after_supervisor_crash(service):
    root, _ = service
    state, helper = root / "managed", root / "worker.py"
    script = (
        f"import sys,time,os;sys.path.insert(0,{str(SOURCE.parent)!r});"
        f"from browser_driver import call;from pathlib import Path;"
        f"call(Path({str(state)!r}),Path({str(helper)!r}),{{'action':'hang'}},time.time()+30,os.environ)"
    )
    client = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(300):
            if (root / "dispatches").exists() and (root / "dispatches").read_text() == "hang\n":
                break
            time.sleep(.01)
        assert (root / "dispatches").read_text() == "hang\n"
        os.kill(int((state / driver.PID_NAME).read_text()), signal.SIGKILL)
        assert client.wait(timeout=3) != 0
        time.sleep(1.2)
        assert not (root / "late").exists()
        assert (root / "dispatches").read_text() == "hang\n"
    finally:
        if client.poll() is None:
            client.kill()
        client.wait(timeout=3)
        stop_managed(state)
