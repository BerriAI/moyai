"""A computer-local process supervisor; no operator or broker credentials."""
import base64
import json
import os
from pathlib import Path
import signal
import select
import socket
import subprocess
import sys
import tempfile
import time

from sandbox.durable_process import atomic


def signal_group(process, value):
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        pass


def input_receipt(process, body):
    if body.get('stop'):
        signal_group(process, signal.SIGKILL)
        return {'accepted': True, 'signal': 'SIGKILL'}
    raw = base64.b64decode(body.get('data_base64', ''), validate=True)
    count, deadline = 0, time.monotonic()+1
    while count < len(raw) and process.poll() is None:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not select.select([], [process.stdin], [], remaining)[1]:
            break
        try:
            count += os.write(process.stdin.fileno(), raw[count:])
        except BlockingIOError:
            continue
        except BrokenPipeError:
            break
    complete = count == len(raw)
    if complete and body.get('eof') and not process.stdin.closed:
        process.stdin.close()
    # Partial bytes are an acknowledged prefix, not an unknown write. Return
    # the exact remaining suffix, including a split UTF-8 sequence, for repair.
    return {'accepted': complete, 'accepted_bytes': count, 'input_bytes': len(raw),
            'eof_sent': complete and bool(body.get('eof')),
            **({'remaining_data_base64': base64.b64encode(raw[count:]).decode(),
                'instruction': 'Only the acknowledged prefix was sent. Send remaining_data_base64 as data_base64 to continue; retain EOF until all bytes are sent.'} if not complete else {})}


def run(root):
    request = json.loads((root/'request.json').read_text())
    socket_path = str(Path(tempfile.gettempdir())/('moyai-command-'+root.name+'.sock'))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(socket_path)
        os.chmod(socket_path, 0o600)
        server.listen()
        server.settimeout(.1)
        with (root/'output.log').open('ab', buffering=0) as output:
            process = subprocess.Popen(['/bin/bash', '-c', request['command']], cwd=request['cwd'],
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            os.set_blocking(process.stdin.fileno(), False)
            atomic(root/'state.json', {'pid': process.pid, 'socket': socket_path})
            try:
                while process.poll() is None:
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(5)
                        try:
                            with connection.makefile('rb') as stream:
                                body = json.loads(stream.readline())
                            result = {**input_receipt(process, body), 'process_id': root.name}
                        except (ValueError, TypeError, AttributeError, OSError):
                            result = {'error': 'Command input could not be confirmed. Do not replay it automatically.'}
                        connection.sendall(json.dumps(result).encode()+b'\n')
                code = process.wait()
                # The handle owns the group, including shell descendants.
                # Services stay alive by running as the foreground command.
                signal_group(process, signal.SIGKILL)
                atomic(root/'done.json', {'status': 'completed', 'exit_code': code})
            finally:
                Path(socket_path).unlink(missing_ok=True)
                signal_group(process, signal.SIGKILL)


if __name__ == '__main__':
    run(Path(sys.argv[1]))
