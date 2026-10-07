"""Private execution service inside a Substrate actor; never run on the host.

Commands live in detached processes with disk journals. Before FULL snapshots,
freeze every other process. On a clone, kill these frozen processes before
accepting work; on the original actor, resume them. This preserves the root
filesystem without replaying a parent's tools or copying its live capabilities.
"""
import base64
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from . import computer
    from .substrate_protocol import VERSION, MAX_BODY, CHUNK, canonical, public_key
except ImportError:
    import computer
    from substrate_protocol import VERSION, MAX_BODY, CHUNK, canonical, public_key

ROOT = Path('/var/lib/moyai-runtime')
IDENTITY = Path('/run/moyai/uid')
LOCK = threading.RLock()
COMPUTER_IDLE = threading.Condition(LOCK)
COMPUTER_PENDING = 0
COMPUTER_FREEZING = False
NONCES = {}
BOOT_ID = os.urandom(16).hex()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.next')
    with tmp.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


def process_identity(pid):
    try:
        # comm can contain spaces and parentheses; fields after its final ')'.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19], fields[0]
    except (OSError, IndexError):
        return None, None


def activate():
    marker = ROOT / 'frozen.json'
    if not marker.exists():
        return False
    saved = json.loads(marker.read_text())
    clone = saved['uid'] != IDENTITY.read_text().strip()
    for pid, start in saved['processes'].items():
        if process_identity(pid)[0] == start:
            try:
                os.kill(int(pid), signal.SIGKILL if clone else signal.SIGCONT)
            except ProcessLookupError:
                pass
    if clone:
        # Per-exec environments can contain the parent's run capability.
        shutil.rmtree(ROOT / 'jobs', ignore_errors=True)
        # Keep commands blocked until exec has discarded the HTTP server's
        # inherited heap, which can also contain previous request credentials.
        return True
    marker.unlink()
    return False


def finish_restart(uid):
    if uid != IDENTITY.read_text().strip():
        raise RuntimeError('Actor identity changed during restart')
    (ROOT / 'frozen.json').unlink(missing_ok=True)


def restart_guest():
    os.execv(sys.executable, [sys.executable, __file__, 'restarted', IDENTITY.read_text().strip()])
    raise RuntimeError('Guest restart did not replace the process')


def freeze():
    global COMPUTER_FREEZING
    with COMPUTER_IDLE:
        COMPUTER_FREEZING = True
        try:
            if not COMPUTER_IDLE.wait_for(lambda: COMPUTER_PENDING == 0, timeout=10):
                raise RuntimeError('Computer action is still running; try the checkpoint again.')
            freeze_processes()
        finally:
            COMPUTER_FREEZING = False


def freeze_processes():
    marker = ROOT / 'frozen.json'
    if marker.exists():
        return
    saved = {'uid': IDENTITY.read_text().strip(), 'processes': {}}
    try:
        for _ in range(10):
            found = False
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit() or int(entry.name) in {1, os.getpid()}:
                    continue
                start, state = process_identity(entry.name)
                if start and state != 'Z' and entry.name not in saved['processes']:
                    found = True
                    saved['processes'][entry.name] = start
                    atomic(marker, saved)
                    try:
                        os.kill(int(entry.name), signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            if not found and all(process_identity(pid)[1] in {None, 'T', 't', 'Z'} for pid in saved['processes']):
                atomic(marker, saved)
                return
            time.sleep(.02)
        raise RuntimeError('Processes did not quiesce')
    except BaseException:
        activate()
        raise


def job_path(value):
    if len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid execution ID')
    return ROOT / 'jobs' / value


@contextmanager
def computer_operation(uid, body):
    global COMPUTER_PENDING
    if (not isinstance(body, dict) or set(body) - {'action', 'actor', 'args'} or
            not isinstance(body.get('action'), str) or
            not isinstance(body.get('args', {}), dict) or
            not isinstance(body.get('actor', ''), str) or len(json.dumps(body).encode()) > 65536):
        raise ValueError('Invalid Computer request')
    with COMPUTER_IDLE:
        if not hmac.compare_digest(uid, IDENTITY.read_text().strip()):
            raise PermissionError('Actor identity changed')
        if COMPUTER_FREEZING or (ROOT / 'frozen.json').exists():
            raise RuntimeError('Workspace checkpoint in progress')
        COMPUTER_PENDING += 1
    try:
        yield
    finally:
        with COMPUTER_IDLE:
            COMPUTER_PENDING -= 1
            COMPUTER_IDLE.notify_all()


def dispatch(path, body):
    with LOCK:
        if path == '/activate':
            if activate():
                restart_guest()
            if body.get('expires_at'):
                uid = IDENTITY.read_text().strip()
                lease_file = ROOT / 'lease.json'
                lease = json.loads(lease_file.read_text()) if lease_file.exists() else {}
                if lease.get('uid') != uid:
                    atomic(lease_file, {'uid': uid, 'expires_at': min(float(body['expires_at']), time.time() + 86400)})
            return {'version': VERSION, 'boot_id': BOOT_ID}
        if path == '/freeze':
            freeze()
            return {'ok': True}
        if (ROOT / 'frozen.json').exists():
            raise RuntimeError('Workspace checkpoint in progress')
        if path == '/start':
            directory = job_path(body['id'])
            if not directory.exists():
                directory.mkdir(parents=True, mode=0o700)
                atomic(directory / 'spec.json', body)
                proc = subprocess.Popen([sys.executable, __file__, 'run', str(directory)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
                atomic(directory / 'pid.json', {'pid': proc.pid})
            return {'id': body['id']}
        if path == '/read':
            directory = job_path(body['id'])
            result = {}
            for name in ('stdout', 'stderr'):
                offset = int(body.get(name, 0))
                if offset < 0:
                    raise ValueError('Invalid offset')
                try:
                    with (directory / name).open('rb') as stream:
                        stream.seek(offset)
                        result[name] = base64.b64encode(stream.read(CHUNK // 2)).decode()
                except FileNotFoundError:
                    result[name] = ''
            done = directory / 'done.json'
            result['exit_code'] = json.loads(done.read_text())['exit_code'] if done.exists() else None
            return result
        if path == '/file/write':
            target = Path(body['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            offset = int(body.get('offset', 0))
            if offset < 0:
                raise ValueError('Invalid offset')
            with target.open('r+b' if offset else 'wb') as stream:
                stream.seek(offset)
                stream.write(base64.b64decode(body['data'], validate=True))
            return {'ok': True}
        if path == '/file/stat':
            return {'size': Path(body['path']).stat().st_size}
        if path == '/file/read':
            with Path(body['path']).open('rb') as stream:
                stream.seek(max(0, int(body.get('offset', 0))))
                return {'data': base64.b64encode(stream.read(CHUNK)).decode()}
        raise ValueError('Unknown operation')


class Handler(BaseHTTPRequestHandler):
    # Close requests before snapshotting: a checkpoint must not resurrect a
    # keepalive socket belonging to another actor or an old web worker.
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        pass  # Request metadata must not disclose execution capabilities.

    def reply(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == '/health' else 404, {'version': VERSION})

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= (65536 if self.path == '/computer' else MAX_BODY):
                return self.reply(413, {'error': 'Request too large'})
            self.connection.settimeout(30)
            data = self.rfile.read(size)
            uid, stamp, nonce = (self.headers.get('X-Moyai-' + key, '') for key in ('Actor', 'Time', 'Nonce'))
            if (not hmac.compare_digest(uid, IDENTITY.read_text().strip()) or
                    abs(time.time() - int(stamp)) > 60 or len(nonce) != 32):
                raise ValueError('Authentication failed')
            public_key(os.environ['MOYAI_SUBSTRATE_PUBLIC_KEY']).verify(
                base64.b64decode(self.headers['X-Moyai-Signature'], validate=True),
                canonical(uid, stamp, nonce, self.path, data))
            with LOCK:
                for key in list(NONCES):
                    if NONCES[key] < time.time() - 120:
                        del NONCES[key]
                if nonce in NONCES or len(NONCES) >= 10000:
                    raise ValueError('Authentication failed')
                NONCES[nonce] = time.time()
        except Exception:
            return self.reply(401, {'error': 'Authentication failed'})
        try:
            body = json.loads(data)
            if self.path == '/computer':
                # Do not journal typed text or hold the guest-wide lock while
                # the desktop acts. Freeze still waits through the reply.
                with computer_operation(uid, body):
                    result = computer.request(body, start=body['action'] not in {'state', 'finish', 'release'})
                    with LOCK:
                        if not hmac.compare_digest(uid, IDENTITY.read_text().strip()):
                            return self.reply(401, {'error': 'Authentication failed'})
                        self.reply(200, result)
                return
            with LOCK:
                # A request authenticated before a snapshot may have waited for
                # the lock. Recheck the projected UID after restoring a clone.
                if not hmac.compare_digest(uid, IDENTITY.read_text().strip()):
                    return self.reply(401, {'error': 'Authentication failed'})
                self.reply(200, dispatch(self.path, body))
        except PermissionError:
            self.reply(401, {'error': 'Authentication failed'})
        except FileNotFoundError:
            self.reply(404, {'error': 'File or execution not found'})
        except Exception:
            self.reply(409, {'error': 'Sandbox operation failed'})


def run(directory):
    spec = json.loads((directory / 'spec.json').read_text())
    code = 1
    try:
        with (directory / 'stdout').open('wb') as out, (directory / 'stderr').open('wb') as err:
            child = subprocess.Popen(spec['command'], env={**os.environ, **spec.get('env', {})},
                                     stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                code = child.wait(timeout=spec.get('timeout'))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                code = 124
    finally:
        (directory / 'spec.json').unlink(missing_ok=True)
        atomic(directory / 'done.json', {'exit_code': code})


def expire():
    """Bound orphaned compute even if the Moyai server never reconnects."""
    observed, unclaimed_until = '', 0
    while True:
        time.sleep(2)
        try:
            uid = IDENTITY.read_text().strip()
            if uid != observed:
                observed, unclaimed_until = uid, time.time() + 300
            lease = json.loads((ROOT / 'lease.json').read_text()) if (ROOT / 'lease.json').exists() else {}
            deadline = lease['expires_at'] if lease.get('uid') == uid else unclaimed_until
            if time.time() > deadline:
                os._exit(0)
        except (OSError, ValueError, KeyError):
            continue


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'run':
        run(Path(sys.argv[2]))
    else:
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if len(sys.argv) == 3 and sys.argv[1] == 'restarted':
            finish_restart(sys.argv[2])
        public_key(os.environ['MOYAI_SUBSTRATE_PUBLIC_KEY'])
        threading.Thread(target=expire, daemon=True).start()
        ThreadingHTTPServer(('0.0.0.0', 80), Handler).serve_forever()
