"""Real loopback-only OpenSSH rehearsal using temporary synthetic credentials.

Run: python -m scripts.release.rehearse_ssh
Requires OpenSSH client, ssh-keygen and sshd. Never contacts Render or production.
"""
from contextlib import ExitStack
import getpass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from scripts.release.ssh_connections import ConnectionFailure, SSHConnections


PROBE = '''import json, os
print('MOYAI_RELEASE_PROBE=' + json.dumps({'ok': True,
    'identity': os.environ['MOYAI_SSH_TEST_INSTANCE'], 'pid': os.getpid()}))
'''


class LocalSSH:
    def __init__(self, directory, identity, *, port=0, credentials=None):
        self.directory, self.identity = Path(directory), identity
        self.directory.mkdir()
        self.credentials = Path(credentials) if credentials else self.directory
        if credentials is None:
            for name in ('host', 'client'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                    '-f', str(self.directory / name)], check=True, capture_output=True)
        self.port = port
        if not port:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                self.port = listener.getsockname()[1]
        self.host = getpass.getuser() + '@127.0.0.1'
        known_hosts = self.directory / 'known_hosts'
        known_hosts.write_text('[127.0.0.1]:' + str(self.port) + ' '
                              + (self.credentials / 'host.pub').read_text())
        config = self.directory / 'sshd_config'
        config.write_text('\n'.join([
            'ListenAddress 127.0.0.1', 'Port ' + str(self.port),
            'HostKey ' + str(self.credentials / 'host'),
            'AuthorizedKeysFile ' + str(self.credentials / 'client.pub'),
            'PidFile ' + str(self.directory / 'pid'), 'UsePAM no', 'StrictModes no',
            'PasswordAuthentication no', 'KbdInteractiveAuthentication no',
            'PermitRootLogin yes', 'LogLevel VERBOSE',
            'AllowUsers ' + getpass.getuser(), 'SetEnv MOYAI_SSH_TEST_INSTANCE=' + identity,
        ]) + '\n')
        self.log = open(self.directory / 'sshd.log', 'w+')
        daemon = shutil.which('sshd') or '/usr/sbin/sshd'
        self.process = subprocess.Popen([daemon, '-D', '-e', '-f', str(config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log,
            start_new_session=True)
        self.args = ['ssh', '-F', '/dev/null', '-v', '-T', '-p', str(self.port),
            '-i', str(self.credentials / 'client'), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'UserKnownHostsFile=' + str(known_hosts), '-o', 'ConnectTimeout=2',
            self.host, sys.executable + ' -']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.close()
                raise RuntimeError('Local sshd could not start; check local OpenSSH server availability.')
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.02)
        self.close()
        raise RuntimeError('Local sshd did not start listening.')

    @property
    def connections(self):
        return (self.directory / 'sshd.log').read_text().count('Accepted publickey for ')

    def stop_listener(self):
        self.process.terminate()
        self.process.wait(timeout=3)

    def close(self):
        # The foreground listener and its accepted children remain ours even
        # when stop_listener models a replacement while old sessions continue.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        self.log.close()


def result(value):
    assert value.returncode == 0
    return json.loads(value.stdout.removeprefix('MOYAI_RELEASE_PROBE='))


def rehearse(emit=print):
    with tempfile.TemporaryDirectory(prefix='moyai-local-ssh-') as directory, ExitStack() as stack:
        old = LocalSSH(Path(directory) / 'old', 'old')
        stack.callback(old.close)
        worker = SimpleNamespace(id='synthetic-worker', deploy_id='deploy-old')
        broker = SimpleNamespace(id='synthetic-broker', deploy_id='deploy-broker')
        for _ in range(3):
            result(subprocess.run([*old.args[:-2], '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                *old.args[-2:]], input=PROBE, text=True, capture_output=True, timeout=5))
        assert old.connections == 3
        emit('BEFORE: 3 fresh observations required 3 authenticated SSH connections.')
        pool = stack.enter_context(SSHConnections())
        start = old.connections
        values = [result(pool.run(worker, old.args, input=PROBE, timeout=5)) for _ in range(3)]
        assert old.connections - start == 1
        assert len({v['pid'] for v in values}) == 3 and {v['identity'] for v in values} == {'old'}
        emit('AFTER: 3 fresh remote interpreters used 1 authenticated connection.')
        result(pool.run(broker, old.args, input=PROBE, timeout=5))
        unaffected = pool.masters[(broker.id, broker.deploy_id)]
        master = pool.masters[(worker.id, worker.deploy_id)]
        count = old.connections
        client = pool._client
        def missing_socket(args, path, *, check=False):
            if not check:
                path.unlink()
            return client(args, path, check=check)
        pool._client = missing_socket
        try:
            pool.run(worker, old.args, input=PROBE, timeout=5)
            raise AssertionError('Missing mux socket unexpectedly succeeded')
        except ConnectionFailure as error:
            assert error.result['transport']['authenticated'] is True
            assert error.result['transport']['command_requested'] is False
        finally:
            pool._client = client
        assert old.connections == count and master.process.poll() is not None
        emit('FAIL CLOSED: disappearing mux socket opened 0 fallback SSH connections.')
        pool.invalidate(worker)
        result(pool.run(worker, old.args, input=PROBE, timeout=5))
        old.stop_listener()
        replacement = LocalSSH(Path(directory) / 'new', 'new', port=old.port, credentials=old.credentials)
        stack.callback(replacement.close)
        assert result(pool.run(worker, old.args, input=PROBE, timeout=5))['identity'] == 'old'
        pool.invalidate(worker)
        worker.deploy_id = 'deploy-new'
        assert result(pool.run(worker, replacement.args, input=PROBE, timeout=5))['identity'] == 'new'
        assert result(pool.run(broker, old.args, input=PROBE, timeout=5))['identity'] == 'old'
        assert pool.masters[(broker.id, broker.deploy_id)] is unaffected
        emit('REPLACEMENT: selected role observed new identity; unaffected role retained its connection.')
        master = pool.masters[(worker.id, worker.deploy_id)]
        count = replacement.connections
        master.process.terminate()
        master.process.wait(timeout=3)
        failed = subprocess.run(pool._client(replacement.args, master.path),
            input=PROBE, text=True, capture_output=True, timeout=5)
        assert failed.returncode != 0 and replacement.connections == count
        emit('DISCONNECT: dead mux master opened 0 fallback SSH connections.')
        pool.invalidate(worker)
        directory_path = Path(pool.directory.name)
        processes = [m.process for m in pool.masters.values()]
        pool.close()
        assert not directory_path.exists() and all(p.poll() is not None for p in processes)
        emit('CLEANUP: all owned masters exited and private control sockets were removed.')
    return {'standalone_connections': 3, 'pooled_connections': 1, 'fallback_connections': 0,
            'fresh_interpreters': 3, 'replacement_identity_verified': True, 'cleanup_verified': True}


if __name__ == '__main__':
    print(json.dumps(rehearse(), sort_keys=True))
