"""Detached, launch-once Hermes supervisor. Only runs inside the sandbox.

The filesystem journal survives a control-plane/Temporal worker disconnect.
An abandoned started marker is uncertain, never permission to repeat a tool.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def supervise(directory, command):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if (directory / 'started.json').exists():
            return
        atomic(directory / 'started.json', {'pid': os.getpid()})
        # Keep third-party diagnostics inside Modal, out of workflow history.
        with (directory / 'stderr.log').open('ab') as stderr, (directory / 'events.jsonl').open('ab') as journal:
            try:
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
                for line in child.stdout:
                    if not line.startswith(b'WORKSPACE_EVENT '):
                        continue
                    try:
                        event = json.loads(line[len(b'WORKSPACE_EVENT '):])
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if event.get('kind') == 'final':
                        atomic(directory / 'final.json', event)
                    journal.write(json.dumps(event).encode() + b'\n')
                    journal.flush()
                    os.fsync(journal.fileno())
                atomic(directory / 'done.json', {'exit_code': child.wait()})
            except Exception:
                # The started marker prevents an ambiguous subprocess failure
                # from being turned into an automatic second execution.
                atomic(directory / 'done.json', {'exit_code': 1})


def status(directory, cursor=0):
    events = []
    journal = directory / 'events.jsonl'
    if journal.exists():
        with journal.open('rb') as handle:
            handle.seek(cursor)
            for _ in range(30):
                line = handle.readline()
                if not line.endswith(b'\n'):
                    break
                events.append(json.loads(line))
                cursor = handle.tell()
    result = {'events': events, 'cursor': cursor, 'state': 'new'}
    if (directory / 'final.json').exists():
        result['final'] = json.loads((directory / 'final.json').read_text())
    if (directory / 'done.json').exists():
        result.update(json.loads((directory / 'done.json').read_text()), state='done')
    elif (directory / 'started.json').exists():
        with (directory / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result['state'] = 'uncertain'
            except BlockingIOError:
                result['state'] = 'running'
    return result


def start(directory, spec_path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Two callers may race to launch; supervise's flock and started marker
    # arbitrate before either child can execute agent code.
    subprocess.Popen([sys.executable, __file__, 'run', str(directory), str(spec_path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


if __name__ == '__main__':
    operation, directory = sys.argv[1], Path(sys.argv[2])
    if operation == 'run':
        supervise(directory, ['/opt/hermes-env/bin/python', '/opt/workspace-runner/sandbox/agent.py', sys.argv[3]])
    elif operation == 'start':
        start(directory, sys.argv[3])
    elif operation == 'read':
        print(json.dumps(status(directory, int(sys.argv[3]))), flush=True)
    else:
        raise SystemExit('Unknown supervisor operation')
