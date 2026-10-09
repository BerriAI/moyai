"""Session-owned, credential-free Codex process reuse inside a warm sandbox.

The lease socket stays open for the entire invocation. Losing its owner kills
the runtime; only an explicit clean release permits reuse. No turns are retried.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress


ROOT = Path('/run/moyai-codex')
STARTUP_SECONDS = 15
MAX_CONTROL_BYTES = 16384
SAFE_ENV = {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}


def clean_env():
    return {key: value for key, value in os.environ.items() if key in SAFE_ENV}


def discard_orphan(root=ROOT):
    """Scrub native files copied by a snapshot, including for cold/child runs."""
    root = Path(root)
    if not root.exists():
        return
    with (root / 'server.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # A live supervisor still owns its native state.
        shutil.rmtree(root / 'home', ignore_errors=True)


def read_message(stream):
    raw = stream.readline(MAX_CONTROL_BYTES + 1)
    if not raw.endswith(b'\n') or len(raw) > MAX_CONTROL_BYTES:
        raise ConnectionError('Runtime lease closed or invalid')
    return json.loads(raw)


def write_message(stream, value):
    stream.write(json.dumps(value).encode() + b'\n')
    stream.flush()


def revision(binary):
    info = Path(binary).stat()
    return hashlib.sha256(json.dumps([str(binary), info.st_size, info.st_mtime_ns,
        Path(__file__).read_text(), Path(__file__).with_name('codex_catalog.py').read_text()]).encode()).hexdigest()


class RuntimeLease:
    """Prewarm concurrently with workspace setup; acquire once before inference."""
    def __init__(self, scope, idle_seconds, *, root=ROOT, model='gpt-6-astra'):
        self.root, self.scope = Path(root), scope
        self.model = model.removeprefix('openai/')
        self.idle_seconds = min(300, max(0, idle_seconds))
        self.connection = self.stream = self.lock = None
        self.info = None
        self.clean = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codex-prewarm')
        self.future = self.pool.submit(self._acquire)

    def _acquire(self):
        from codex_cli_bin import bundled_codex_path
        binary = str(bundled_codex_path())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / 'lease.lock').open('a')
        # Overlapping requests must not share a runtime or stop its owner.
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + STARTUP_SECONDS
        started = False
        while True:
            connection = socket.socket(socket.AF_UNIX)
            connection.settimeout(STARTUP_SECONDS)
            try:
                connection.connect(str(self.root / 'lease.sock'))
                break
            except OSError:
                connection.close()
                if not started:
                    subprocess.Popen([sys.executable, __file__, 'serve', str(self.root), binary],
                        env=clean_env(), cwd=str(self.root), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True, close_fds=True)
                    started = True
                if time.monotonic() >= deadline:
                    raise TimeoutError('Codex prewarm unavailable')
                time.sleep(.02)
        self.connection = connection
        self.stream = connection.makefile('rwb')
        write_message(self.stream, {'scope': self.scope, 'revision': revision(binary),
                                   'model': self.model, 'idle_seconds': self.idle_seconds})
        self.info = read_message(self.stream)
        if self.info.get('error'):
            raise RuntimeError('Codex prewarm failed')
        connection.settimeout(None)
        return self.info

    def ready(self):
        try:
            return self.future.result(timeout=STARTUP_SECONDS + 1)
        except Exception:
            return None  # Before inference only: the normal cold path remains usable.

    def close(self):
        try:
            self.pool.shutdown(wait=True)
            if self.info and self.stream:
                self.connection.settimeout(5)
                write_message(self.stream, {'clean': self.clean})
                read_message(self.stream)
        except Exception:
            pass  # A missing clean release makes the supervisor discard the process.
        finally:
            # Disconnect even if a buffered stream cannot flush/close. Every
            # handle gets its own close attempt; release is safe to repeat.
            with suppress(Exception):
                if self.connection:
                    self.connection.shutdown(socket.SHUT_RDWR)
            for name in ('stream', 'connection', 'lock'):
                resource = getattr(self, name)
                setattr(self, name, None)
                with suppress(Exception):
                    if resource:
                        resource.close()


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def serve(root, binary):
    from codex_cli_bin import bundled_path_dir
    root = Path(root)
    server_revision = revision(binary)
    # A stale socket from a filesystem snapshot is never a live runtime.
    with (root / 'server.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        control = root / 'lease.sock'
        control.unlink(missing_ok=True)
        process, contract = None, None
        idle_seconds = STARTUP_SECONDS
        home = root / 'home'
        endpoint = root / 'runtime.sock'
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(control))
            control.chmod(0o600)
            server.listen(1)
            try:
                while True:
                    server.settimeout(idle_seconds)
                    try:
                        connection, _ = server.accept()
                    except TimeoutError:
                        break
                    with connection, connection.makefile('rwb') as stream:
                        try:
                            connection.settimeout(STARTUP_SECONDS)
                            request = read_message(stream)
                            if request['revision'] != server_revision:
                                write_message(stream, {'error': 'runtime_updated'})
                                return  # A refreshed adapter must not reuse an old supervisor.
                            requested = (request['scope'], request['revision'], request['model'])
                            reused = process is not None and process.poll() is None and contract == requested
                            if not reused:
                                stop_process(process)
                                process = None
                                shutil.rmtree(home, ignore_errors=True)
                                home.mkdir(mode=0o700)
                                endpoint.unlink(missing_ok=True)
                                env = {**clean_env(), 'CODEX_HOME': str(home)}
                                if bundled_path_dir():
                                    env['PATH'] = str(bundled_path_dir()) + os.pathsep + env.get('PATH', '')
                                result = subprocess.run([binary, 'debug', 'models', '--bundled'],
                                    env=env, capture_output=True, text=True, check=True, timeout=10)
                                catalog = home / 'bundled-models.json'
                                catalog.write_text(json.dumps(json.loads(result.stdout)))
                                try:
                                    from .codex_catalog import search_catalog
                                except ImportError:
                                    from codex_catalog import search_catalog
                                selected = search_catalog(binary, home, request['model'], env, cached=catalog)
                                # No broker URL, capability, project config or model turn
                                # belongs in this process's initial environment/config.
                                settings = ['thread_unload_delay_secs=0', 'features.plugins=false',
                                    'features.apps=false', 'features.hooks=false', 'features.memories=false',
                                    'project_doc_max_bytes=0', 'cli_auth_credentials_store="ephemeral"',
                                    'history.persistence="none"']
                                if selected:
                                    settings.append('model_catalog_json=' + json.dumps(selected))
                                process = subprocess.Popen([binary,
                                    *[part for value in settings for part in ('-c', value)],
                                    'app-server', '--listen', 'unix://' + str(endpoint)],
                                    env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                deadline = time.monotonic() + 10
                                while not endpoint.exists():
                                    if process.poll() is not None or time.monotonic() >= deadline:
                                        raise RuntimeError('Codex runtime did not start')
                                    time.sleep(.01)
                                contract = requested
                            idle_seconds = min(300, max(.01, float(request['idle_seconds'])))
                            write_message(stream, {'socket': str(endpoint), 'catalog': str(home / 'bundled-models.json'),
                                                   'pid': process.pid, 'reused': reused})
                            connection.settimeout(None)
                            release = read_message(stream)
                            if release != {'clean': True}:
                                stop_process(process)
                                process, contract = None, None
                            write_message(stream, {'released': True})
                        except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError):
                            stop_process(process)
                            process, contract = None, None
            finally:
                stop_process(process)
                shutil.rmtree(home, ignore_errors=True)
                control.unlink(missing_ok=True)
                endpoint.unlink(missing_ok=True)


def proxy(endpoint):
    """Frame SDK JSON lines over Codex's native Unix WebSocket transport."""
    from websockets.sync.client import unix_connect
    from websockets.exceptions import ConnectionClosed
    with unix_connect(endpoint, max_size=None, open_timeout=10) as connection:
        def send():
            try:
                for line in sys.stdin:
                    connection.send(line.rstrip('\n'))
            except ConnectionClosed:
                pass
            finally:
                connection.close()
        threading.Thread(target=send, daemon=True).start()
        for message in connection:
            print(message, flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'serve':
        serve(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == 'proxy':
        proxy(sys.argv[2])
