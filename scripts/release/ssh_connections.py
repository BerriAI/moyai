"""Owned OpenSSH connections for read-only release observations.

Each command still starts a fresh remote interpreter. A connection belongs to one
service/deployment pair, and no command is replayed inside this transport.
"""
from collections import deque
from contextlib import contextmanager
import hashlib
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from scripts.release.errors import ReleaseError
from scripts.release.ssh_diagnostics import TRANSIENT_KINDS, failure


@contextmanager
def defer_cancellation():
    # A signal handler can raise between spawning and registering the master.
    # Defer only Python handlers for this short ownership transfer. Unlike a
    # blocked signal mask, this does not make child ssh ignore termination.
    handlers, pending = {}, []
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            if callable(handler):
                handlers[signum] = handler
                signal.signal(signum, lambda sig, frame: pending.append((sig, frame)))
    try:
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if pending:
            signum, frame = pending[0]
            handlers[signum](signum, frame)


class ConnectionFailure(Exception):
    def __init__(self, result):
        super().__init__('SSH connection could not complete an observation')
        self.result = result


class Master:
    def __init__(self, args, path):
        self.path = path
        self.lines, self.authenticated = deque(maxlen=16), False
        self.security = set()
        self.lock = threading.Lock()
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.reader = threading.Thread(target=self.collect, daemon=True)
        try:
            self.reader.start()
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=2)
            self.process.stderr.close()
            self.path.unlink(missing_ok=True)
            raise

    def collect(self):
        # Bounded, memory-only diagnostics. Never persist command arguments,
        # hostnames, key paths or arbitrary provider output.
        for line in iter(lambda: self.process.stderr.readline(4096), b''):
            with self.lock:
                self.authenticated |= b'Authenticated to ' in line
                for marker in (b'Permission denied', b'Too many authentication failures',
                               b'Host key verification failed', b'REMOTE HOST IDENTIFICATION HAS CHANGED'):
                    if marker in line:
                        self.security.add(marker)
                self.lines.append(line)

    def diagnostics(self):
        with self.lock:
            return ((b'Authenticated to \n' if self.authenticated else b'')
                    + b'\n'.join(sorted(self.security)) + b''.join(self.lines))

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        # A terminated process closes the pipe. Never block closing a pipe that
        # a still-running reader owns; report cleanup failure to the controller.
        self.reader.join(timeout=2)
        clean = self.process.poll() is not None and not self.reader.is_alive()
        if not self.reader.is_alive():
            self.process.stderr.close()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            clean = False
        return clean


class SSHConnections:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        # Short Unix socket names also fit macOS's 104-byte limit. The private
        # directory is job-local and cannot reuse a previous release's socket.
        self.directory = tempfile.TemporaryDirectory(prefix='moyai-ssh-', dir='/tmp')
        self.masters = {}
        self.clock, self.sleep = clock, sleep
        self.next_connection = 0
        self.pre_auth_failures = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        self.close(raise_error=exc_type is None)

    def close(self, *, raise_error=True):
        clean = True
        for key in list(self.masters):
            clean = self._close(key) and clean
        try:
            self.directory.cleanup()
        except OSError:
            clean = False
        if not clean and raise_error:
            raise ReleaseError('SSH observation connection cleanup failed.')

    def _close(self, key):
        master = self.masters.get(key)
        clean = master.close() if master is not None else True
        if clean:
            self.masters.pop(key, None)
        return clean

    def invalidate(self, service, *, raise_error=True):
        clean = True
        for key in list(self.masters):
            if key[0] == service.id:
                clean = self._close(key) and clean
        if not clean and raise_error:
            raise ReleaseError('SSH observation connection cleanup failed.')

    def _failed(self, started, *, master=None, stdout=None, stderr=None, returncode=None, kind=None):
        if isinstance(stderr, str):
            stderr = stderr.encode()
        if master is not None:
            # Give an exited master's collector time to retain host/auth errors
            # before classifying a generic mux disconnect. Raw bytes stay local.
            if master.process.poll() is not None:
                master.reader.join(timeout=.2)
            # Sending-command lines belong to earlier mux sessions. Current
            # session admission is recorded only by this client's diagnostics.
            inherited = b'\n'.join(line for line in master.diagnostics().splitlines()
                                   if b'Sending command:' not in line)
            stderr = inherited + b'\n' + (stderr or b'')
        result = failure(stdout=stdout, stderr=stderr, returncode=returncode,
            marker='MOYAI_RELEASE_PROBE=', elapsed_ms=round((self.clock() - started) * 1000), kind=kind)
        details = result['transport']
        if (result['reason'] == 'ssh_connection_lost' and details['kind'] in TRANSIENT_KINDS
                and not details['authenticated'] and not details['command_requested']):
            self.pre_auth_failures += 1
            # This is a bounded recovery delay, not a claimed provider limit.
            self.next_connection = self.clock() + min(20, 10 * self.pre_auth_failures)
        return ConnectionFailure(result)

    def _remaining(self, deadline, started, master=None):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise self._failed(started, master=master, kind='command_timeout')
        return remaining

    @staticmethod
    def _client(args, path, *, check=False):
        # OpenSSH otherwise silently falls back to a direct connection if a
        # socket disappears. The fixed false proxy makes fallback fail locally;
        # only _master may open a connection. Ignore ambient client config.
        return [*args[:-2], '-o', 'ControlMaster=no', '-o', 'ControlPersist=no',
                '-o', 'ProxyCommand=/bin/false', '-S', str(path),
                *(['-O', 'check', args[-2]] if check else args[-2:])]

    def _master(self, service, args, deadline, started):
        key = (service.id, service.deploy_id)
        for previous in list(self.masters):
            if previous[0] == service.id and previous != key:
                if not self._close(previous):
                    raise ReleaseError('SSH observation connection cleanup failed.')
        master = self.masters.get(key)
        if master is not None and master.process.poll() is None and master.path.exists():
            return master
        if not self._close(key):
            raise ReleaseError('SSH observation connection cleanup failed.')
        delay = max(0, self.next_connection - self.clock())
        if delay:
            self.sleep(min(delay, self._remaining(deadline, started)))
        self._remaining(deadline, started)
        path = Path(self.directory.name) / hashlib.sha256(repr(key).encode()).hexdigest()[:24]
        # SIGINT/SIGTERM handlers may raise during construction. Register the
        # owned child before delivering cancellation so context cleanup finds it.
        with defer_cancellation():
            master = Master([*args[:-2], '-M', '-N', '-n', '-o', 'ControlPersist=no',
                             '-S', str(path), args[-2]], path)
            self.masters[key] = master
        try:
            while master.process.poll() is None:
                remaining = self._remaining(deadline, started, master)
                if path.exists():
                    checked = subprocess.run(self._client(args, path, check=True),
                        capture_output=True, timeout=min(1, remaining))
                    if checked.returncode == 0:
                        self.pre_auth_failures = 0
                        self.next_connection = 0
                        return master
                self.sleep(min(.05, remaining))
            raise self._failed(started, master=master, returncode=master.process.poll())
        except subprocess.TimeoutExpired as exc:
            error = self._failed(started, master=master, stderr=exc.stderr, kind='command_timeout')
            self._close(key)
            raise error from None
        except BaseException:
            self._close(key)
            raise

    def run(self, service, args, *, input, timeout):
        started = self.clock()
        master, succeeded = None, False
        try:
            deadline = started + timeout
            master = self._master(service, args, deadline, started)
            result = subprocess.run(self._client(args, master.path), input=input, text=True,
                capture_output=True, timeout=self._remaining(deadline, started, master))
            if result.returncode:
                raise self._failed(started, master=master, stdout=result.stdout,
                    stderr=result.stderr, returncode=result.returncode)
            succeeded = True
            return result
        except subprocess.TimeoutExpired as exc:
            raise self._failed(started, master=master, stdout=exc.stdout,
                stderr=exc.stderr, kind='command_timeout') from None
        except OSError:
            raise self._failed(started, master=master, kind='local_os_error') from None
        finally:
            # A failed read-only command is never replayed here. A subsequent
            # bounded controller retry must establish a new connection.
            if not succeeded:
                self.invalidate(service, raise_error=False)
