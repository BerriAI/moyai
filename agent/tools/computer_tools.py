"""Workspace primitives shared by local adapters and the remote computer bridge.

Output pages are transport views. Logs and file bytes remain available, and a
command keeps running until completion, an explicit stop, or computer loss.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from .artifact_tools import workspace_path

PAGE_BYTES = 65536


def tool(name, description, properties, required=(), read=False):
    return {'name': 'computer_'+name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False},
            'annotations': {'readOnlyHint': read, 'idempotentHint': read}}


TEXT = {'type': 'string'}
OFFSET = {'type': 'integer', 'minimum': 0, 'default': 0}
LENGTH = {'type': 'integer', 'minimum': 1, 'default': PAGE_BYTES}
TOOLS = [
    tool('read_file', 'Read exact workspace file bytes by offset/length; follow next_offset. UTF-8 text is supplied when the range decodes; base64 always preserves the bytes.',
         {'path': TEXT, 'offset': OFFSET, 'length': LENGTH}, ['path'], True),
    tool('write_file', 'Write complete text or base64 bytes in the workspace. Choose exactly one encoding. Existing files are replaced only with overwrite=true.',
         {'path': TEXT, 'text': TEXT, 'data_base64': TEXT, 'overwrite': {'type': 'boolean', 'default': False}}, ['path']),
    tool('terminal', 'Start a shell command in the isolated computer and return its process handle. Long commands keep running; poll the handle and page its complete output. Run services in the foreground: descendants are stopped when the command exits. A request_key safely identifies this exact launch when retrying an acknowledgment.',
         {'command': TEXT, 'request_key': TEXT, 'yield_ms': OFFSET, 'length': LENGTH}, ['command']),
    tool('terminal_read', 'Read an existing command status and complete output pages. Running commands are not killed when a tool call returns. A lost computer is an unknown outcome; inspect effects before retrying.',
         {'process_id': TEXT, 'offset': OFFSET, 'length': LENGTH, 'yield_ms': OFFSET}, ['process_id'], True),
    tool('terminal_input', 'Send text or EOF to a running command, or forcefully stop its entire process group with stop=true. Input is never automatically replayed; a partial receipt reports exactly how many UTF-8 bytes were accepted.',
         {'process_id': TEXT, 'text': TEXT, 'data_base64': TEXT, 'eof': {'type': 'boolean'}, 'stop': {'type': 'boolean'}}, ['process_id']),
]


class ComputerToolError(ValueError):
    pass


def integer(args, name, default, minimum=0):
    value = args.get(name, default)
    if type(value) is not int or value < minimum:
        raise ComputerToolError(name+' must be an integer at least '+str(minimum)+'.')
    return value


def directory(workspace, identity):
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise ComputerToolError('Use the returned process_id.')
    workspace = Path(workspace).resolve()
    # Logs and command arguments must never be included by a repository's
    # `git add -A`. Production snapshots persist /session independently.
    root = (Path('/session/.computer-processes') if workspace.is_relative_to('/workspace')
            else workspace.parent/'.moyai-processes'/hashlib.sha256(str(workspace).encode()).hexdigest())
    return root/identity


def file_path(path, workspace):
    try:
        return workspace_path(path, workspace)[1]
    except ValueError:
        raise ComputerToolError('File paths must stay inside the workspace.') from None


def rpc(root, body):
    try:
        state = json.loads((root/'state.json').read_text())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(state['socket'])
            connection.sendall(json.dumps(body).encode()+b'\n')
            with connection.makefile('rb') as stream:
                return json.loads(stream.readline())
    except (OSError, ValueError, KeyError):
        raise ComputerToolError('The command is no longer reachable. Its action outcome is unknown; inspect saved output and destination effects before retrying.') from None


def read_file(arguments, workspace, *, append_only=False):
    path = file_path(arguments['path'], workspace)
    return read_path(path, arguments, append_only=append_only)


def read_path(path, arguments, *, append_only=False):
    offset, length = integer(arguments, 'offset', 0), integer(arguments, 'length', PAGE_BYTES, 1)
    with path.open('rb') as source:
        before = os.fstat(source.fileno())
        if offset > before.st_size:
            raise ComputerToolError('offset is past the observed end of the file.')
        source.seek(offset)
        raw = source.read(min(length, before.st_size-offset))
        after = os.fstat(source.fileno())
    if (after.st_size < before.st_size or (not append_only and
            (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns))):
        raise ComputerToolError('The file changed during the read; read a saved artifact revision for immutable paging.')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    end = offset+len(raw)
    return {'path': str(path), 'size': before.st_size, 'offset': offset, 'bytes': len(raw), 'text': text,
            'data_base64': base64.b64encode(raw).decode(), 'next_offset': end if end < before.st_size else None,
            'observed_mtime_ns': before.st_mtime_ns}


def process_read(arguments, workspace):
    root = directory(workspace, arguments['process_id'])
    if not (root/'request.json').is_file():
        raise ComputerToolError('Process not found on this computer.')
    # Waiting yields at a transport boundary; it never bounds the process lifetime.
    deadline = time.monotonic()+integer(arguments, 'yield_ms', 0)/1000
    while not (root/'done.json').exists() and time.monotonic() < deadline:
        time.sleep(min(.05, max(0, deadline-time.monotonic())))
    status = json.loads((root/'done.json').read_text()) if (root/'done.json').exists() else {'status': 'running'}
    if status['status'] == 'running':
        try:
            rpc(root, {'status': True})
        except ComputerToolError:
            status = {'status': 'unknown', 'instruction': 'Command supervisor is unavailable. Inspect effects before retrying.'}
    log = root/'output.log'
    page = read_path(log, {'offset': arguments.get('offset', 0), 'length': arguments.get('length', PAGE_BYTES)}, append_only=True) if log.exists() else {'text': '', 'data_base64': '', 'offset': 0, 'bytes': 0, 'size': 0, 'next_offset': None}
    return {**status, 'process_id': root.name, 'output': page.pop('text'), **page,
            'output_path': str(log), 'poll_offset': page['offset']+page['bytes']}


def terminal(arguments, workspace):
    command = arguments['command']
    if not isinstance(command, str) or not command.strip():
        raise ComputerToolError('Supply a nonempty shell command.')
    key = arguments.get('request_key') or uuid4().hex
    if not isinstance(key, str):
        raise ComputerToolError('request_key must be text.')
    # Credential/execution identity prevents reusing an old turn's launch key.
    identity = hashlib.sha256((os.environ.get('WORKSPACE_RUN_TOKEN', '')+'\0'+str(Path(workspace).resolve())+'\0'+key).encode()).hexdigest()[:32]
    root = directory(workspace, identity)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    with (root/'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = {'command': command, 'cwd': str(Path(workspace).resolve())}
        if (root/'request.json').exists():
            if json.loads((root/'request.json').read_text()) != request:
                raise ComputerToolError('This request_key already identifies a different command.')
        else:
            from sandbox.durable_process import atomic
            atomic(root/'request.json', request)
            # The supervisor receives no provider, broker or operator capabilities.
            env = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'TERM'}}
            env['HOME'] = str(root/'home'); Path(env['HOME']).mkdir(mode=0o700)
            subprocess.Popen([sys.executable, '-m', 'sandbox.command_process', str(root)],
                             cwd=str(Path(__file__).resolve().parents[2]), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    # Brief startup handoff; a slow/lost supervisor remains explicit, never relaunched.
    deadline = time.monotonic()+5
    while not (root/'state.json').exists() and not (root/'done.json').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    return process_read({'process_id': identity, 'yield_ms': arguments.get('yield_ms', 1000), 'length': arguments.get('length', PAGE_BYTES)}, workspace)


def call(name, arguments, workspace):
    spec = next((tool for tool in TOOLS if tool['name'] == name), None)
    if not spec or not isinstance(arguments, dict) or arguments.keys()-spec['inputSchema']['properties'].keys() or not set(spec['inputSchema']['required']) <= arguments.keys():
        raise ComputerToolError('Use the computer tool schema without extra fields.')
    # Validate every field before any filesystem or command effect. SDKs differ
    # in whether they validate custom-tool schemas before dispatching them.
    types = {'string': str, 'boolean': bool, 'integer': int}
    for key, value in arguments.items():
        field = spec['inputSchema']['properties'][key]
        if type(value) is not types[field['type']]:
            raise ComputerToolError(key+' must be a '+field['type']+'.')
        if 'minimum' in field and value < field['minimum']:
            raise ComputerToolError(key+' must be at least '+str(field['minimum'])+'.')
    if 'request_key' in arguments and not arguments['request_key'].strip():
        raise ComputerToolError('request_key must be nonempty text.')
    if name == 'computer_read_file':
        return read_file(arguments, workspace)
    if name == 'computer_write_file':
        path = file_path(arguments['path'], workspace)
        if ('text' in arguments) == ('data_base64' in arguments):
            raise ComputerToolError('Choose exactly one of text or data_base64.')
        if type(arguments.get('overwrite', False)) is not bool:
            raise ComputerToolError('overwrite must be a boolean.')
        try:
            raw = arguments['text'].encode() if 'text' in arguments else base64.b64decode(arguments['data_base64'], validate=True)
        except (ValueError, TypeError, AttributeError):
            raise ComputerToolError('Supply text or valid base64 bytes.') from None
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
        descriptor, staging = tempfile.mkstemp(prefix='.moyai-write-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(staging, mode)
            if arguments.get('overwrite'):
                os.replace(staging, path)
            else:
                # Atomic exclusive publication: a competing writer cannot be
                # overwritten between an existence check and a rename.
                os.link(staging, path)
        finally:
            Path(staging).unlink(missing_ok=True)
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if name == 'computer_terminal':
        return terminal(arguments, workspace)
    if name == 'computer_terminal_read':
        return process_read(arguments, workspace)
    if 'text' in arguments and 'data_base64' in arguments:
        raise ComputerToolError('Choose text or data_base64 for command input.')
    try:
        data = (base64.b64decode(arguments['data_base64'], validate=True) if 'data_base64' in arguments
                else arguments.get('text', '').encode())
    except ValueError:
        raise ComputerToolError('Supply valid base64 command input.') from None
    if arguments.get('stop') and (data or arguments.get('eof')):
        raise ComputerToolError('Use stop by itself, or send text/EOF without stop.')
    if not arguments.get('stop') and not arguments.get('eof') and not data:
        raise ComputerToolError('Supply text, eof=true or stop=true.')
    return rpc(directory(workspace, arguments['process_id']), {
        'data_base64': base64.b64encode(data).decode(),
        'eof': arguments.get('eof', False), 'stop': arguments.get('stop', False)})
