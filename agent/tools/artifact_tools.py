"""Stream exact files between the workspace and the existing artifact owner."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError
from uuid import uuid4


CHUNK_BYTES = 1024 * 1024  # Transport page, not a retention or file-size cap.


class ArtifactError(ValueError):
    """Safe, actionable local transfer errors (never provider response bodies)."""


def transfer(broker, payload):
    # Exact upload/part identities make acknowledgment loss safe to retry. This
    # transport retry budget does not constrain the file or subsequent tool calls.
    for attempt in range(3):
        try:
            return checked(broker('/artifacts/transfer', payload))
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
        except (OSError, json.JSONDecodeError):
            pass
        if attempt < 2:
            time.sleep(0.2 * 2**attempt)
    raise ArtifactError('Artifact transfer acknowledgment is unavailable. Retrying artifact_publish is safe; identical revisions are deduplicated.')


def workspace_path(path, workspace):
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Artifact files must stay inside the workspace.')
    return root, target


def checked(result):
    if isinstance(result, dict) and result.get('error'):
        raise ValueError(result['error'])
    return result


def publish(arguments, broker, workspace):
    root, source = workspace_path(arguments['path'], workspace)
    if not source.is_file():
        raise ValueError('Publish an existing regular file.')
    name = arguments.get('name') or source.relative_to(root).as_posix()
    identity = uuid4().hex
    # Hash first, then transfer from the same open descriptor. The server verifies
    # the complete digest again before publishing, including concurrent edits.
    with source.open('rb') as stream:
        before = os.fstat(stream.fileno())
        checksum = hashlib.sha256()
        secrets = [os.environ[key].encode() for key in ('WORKSPACE_RUN_TOKEN', 'WORKSPACE_ACCESS_CLIENT_SECRET') if os.environ.get(key)]
        carry, span = b'', max((len(secret) for secret in secrets), default=1)-1
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            if any(secret in carry+chunk for secret in secrets):
                raise ValueError('This file contains a workspace credential and cannot be published.')
            checksum.update(chunk)
            carry = (carry+chunk)[-span:] if span else b''
        transfer(broker, {'action': 'begin', 'upload_id': identity, 'path': name,
                         'size': before.st_size, 'sha256': checksum.hexdigest()})
        stream.seek(0)
        parts = 0
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            transfer(broker, {'action': 'part', 'upload_id': identity, 'part': parts,
                             'data': base64.b64encode(chunk).decode()})
            parts += 1
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('The source changed during publication. Retry after the writer finishes.')
    return transfer(broker, {'action': 'commit', 'upload_id': identity, 'parts': parts})


def fetch(arguments, broker, workspace):
    root, destination = workspace_path(arguments['destination'], workspace)
    if destination.exists():
        raise ValueError('Choose a new destination; artifact_fetch never overwrites a file.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging = tempfile.mkstemp(prefix='.artifact-', dir=destination.parent)
    checksum, offset, manifest = hashlib.sha256(), 0, None
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            while True:
                data = checked(broker('/tools/call', {'name': 'artifact_read', 'arguments': {
                    **{key: arguments[key] for key in ('run_id', 'path', 'revision') if key in arguments},
                    'offset': offset, 'length': CHUNK_BYTES}}))
                raw = base64.b64decode(data['data_base64'], validate=True)
                if data['offset'] != offset or data['bytes'] != len(raw) or data['revision'] != arguments['revision']:
                    raise ValueError('Artifact transfer returned inconsistent bytes.')
                if manifest and any(data[key] != manifest[key] for key in ('run_id', 'path', 'revision', 'size')):
                    raise ValueError('Artifact manifest changed during download.')
                manifest = data
                checksum.update(raw)
                stream.write(raw)
                offset += len(raw)
                if data['next_offset'] is None:
                    break
                if not raw or data['next_offset'] != offset:
                    raise ValueError('Artifact transfer did not advance.')
            stream.flush()
            os.fsync(stream.fileno())
        if offset != manifest['size'] or checksum.hexdigest() != manifest['revision']:
            raise ValueError('Artifact download failed complete-file verification.')
        # Atomic no-replace publication also protects against a concurrent writer.
        os.link(staging, destination)
        return {'saved': True, 'path': str(destination), 'bytes': offset, 'revision': manifest['revision']}
    finally:
        Path(staging).unlink(missing_ok=True)


def call(name, arguments, broker, workspace):
    required = {'path'} if name == 'artifact_publish' else {'path', 'revision', 'destination'}
    allowed = required | ({'name'} if name == 'artifact_publish' else {'run_id'})
    if (not isinstance(arguments, dict) or not required <= arguments.keys() or arguments.keys()-allowed
            or any(not isinstance(value, str) for value in arguments.values())
            or any(not arguments[key] for key in required)):
        raise ArtifactError('Supply the required nonempty text fields from the tool schema, without extra fields.')
    try:
        return (publish if name == 'artifact_publish' else fetch)(arguments, broker, workspace)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from None
