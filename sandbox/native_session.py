"""Optional native state; public receipts remain the recovery authority."""
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from uuid import uuid4


MAX_BYTES = 2_000_000
MAX_FILES = 256


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


@contextmanager
def native_storage(root, legacy_home, temporary):
    """Run before project startup, including a live parent's cloned snapshot.

    These are sandbox-owned paths, supplied by the sandbox entry point.
    Project preparation keeps its own temporary directory and lifetime.
    """
    remove(root)
    for path in (legacy_home / '.claude' / 'projects', legacy_home / '.cache' / 'litellm-harness'):
        remove(path)
    for pattern in ('claude-resume-*', 'litellm-harness-*'):
        for path in temporary.glob(pattern):
            remove(path)
    try:
        yield
    finally:
        remove(root)


class NativeSession:
    def __init__(self, context, store, harness, compatibility):
        self.context, self.store = context, store
        self.root = (store.path.parent / '.native-sdk' if store is not None
                     else Path(tempfile.mkdtemp(prefix='moyai-native-')))
        self.cache = self.root / 'home' / '.cache' / 'litellm-harness'
        self.env = {key: str(self.root / path) for key, path in {
            'HOME': 'home', 'TMPDIR': 'tmp', 'TMP': 'tmp', 'TEMP': 'tmp',
            'CLAUDE_CONFIG_DIR': 'claude', 'CODEX_HOME': 'codex',
            'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data',
            'XDG_CACHE_HOME': 'cache', 'XDG_STATE_HOME': 'state',
        }.items()}
        contract = {'version': 1, 'harness': harness, 'cwd': context.cwd, 'adapter': compatibility}
        self.compatibility = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        self.lease = uuid4().hex
        self.state, self.staged = None, None
        self.enabled = False
        self.reason = 'unavailable'

    @property
    def resumed(self):
        return self.state is not None

    def checkpoint(self):
        with self.store.lock:
            return {'epoch': self.store.state()['epoch'],
                    'seq': self.store.db.execute('SELECT coalesce(max(seq),0) FROM journal').fetchone()[0]}

    def reset_files(self):
        remove(self.root)
        for directory in self.env.values():
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def temporary_files(self):
        # The public SessionStore API materializes resumes with the parent's
        # tempfile module before subprocess spawn. Scope it only to SDK work.
        previous = tempfile.tempdir
        tempfile.tempdir = self.env['TMPDIR']
        try:
            yield
        finally:
            tempfile.tempdir = previous

    def begin(self, *, resume=True):
        self.reset_files()
        self.state, self.staged = None, None
        self.enabled = False
        self.lease = uuid4().hex
        self.reason = 'unavailable'
        if self.store is None or not hasattr(self.context.relay, 'native'):
            return None
        try:
            reply = self.context.relay.native({'action': 'begin' if resume else 'restart', 'lease': self.lease,
                'compatibility': self.compatibility, 'checkpoint': self.checkpoint()})
            if reply.get('lease') != self.lease:
                return None
            self.reason = reply.get('reason', 'fresh')
            saved = reply.get('state') if resume else None
            if saved is not None and (not isinstance(saved, dict) or len(json.dumps(saved).encode()) > MAX_BYTES):
                return self.begin(resume=False)
            self.state = saved
            self.enabled = True
            if self.store.pending or self.context.spec.get('fresh_child') or self.context.spec.get('workspace_warning'):
                self.invalidate()
        except Exception:
            # A failed optional load must never replay an SDK invocation.
            self.state = None
            self.enabled = False
        return self.state

    def invalidate(self):
        self.state, self.staged = None, None
        self.reset_files()
        if self.enabled:
            try:
                self.context.relay.native({'action': 'invalidate', 'lease': self.lease})
            except Exception:
                pass
        self.enabled = False

    def finish(self, payload):
        if not self.enabled or self.store.pending:
            return
        try:
            encoded = json.dumps(payload)
            if not isinstance(payload, dict) or len(encoded.encode()) > MAX_BYTES:
                return
            self.staged = (json.loads(encoded), self.checkpoint())
        except (TypeError, ValueError):
            return

    def close(self):
        try:
            if self.staged and self.enabled and not self.store.pending:
                state, checkpoint = self.staged
                if checkpoint == self.checkpoint():
                    self.context.relay.native({'action': 'commit', 'lease': self.lease,
                        'compatibility': self.compatibility, 'checkpoint': checkpoint, 'state': state})
        except Exception:
            pass  # Failed upload loses only an optimization, after the answer.
        finally:
            self.state, self.staged = None, None
            remove(self.root)

    def owned_path(self, path):
        path = Path(path)
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Invalid native directory')
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError('Native snapshots cannot contain symbolic links')
            if part == self.root:
                break
        return path

    def files(self, path):
        path = self.owned_path(path)
        result, size = {}, 0
        for source in sorted(path.rglob('*')):
            if source.is_symlink():
                raise ValueError('Native snapshots cannot contain symbolic links')
            if source.is_dir():
                continue
            if not source.is_file() or source.stat().st_size > MAX_BYTES:
                raise ValueError('Invalid native file')
            with source.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Native snapshot exceeds its limit')
            value = base64.b64encode(raw).decode()
            size += len(value)
            if len(result) >= MAX_FILES or size > MAX_BYTES:
                raise ValueError('Native snapshot exceeds its limit')
            result[source.relative_to(path).as_posix()] = value
        return result

    def restore_files(self, path, files):
        path = self.owned_path(path)
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise ValueError('Invalid native snapshot')
        decoded, size = [], 0
        for name, value in files.items():
            if not isinstance(name, str):
                raise ValueError('Invalid native file name')
            relative = PurePosixPath(name)
            if (not isinstance(value, str) or relative.is_absolute() or '..' in relative.parts
                    or not relative.parts or '\\' in name or '\x00' in name):
                raise ValueError('Invalid native file name')
            data = base64.b64decode(value, validate=True)
            size += len(data)
            if size > MAX_BYTES:
                raise ValueError('Native snapshot exceeds its limit')
            decoded.append((self.owned_path(path / relative), data))
        for target, data in decoded:
            if not target.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('Native snapshot escaped its directory')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(0o600)
