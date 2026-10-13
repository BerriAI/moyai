"""Computer-dependent calls leave the isolated agent runtime through one owner."""
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from uuid import uuid4

from agent.tools.computer_tools import TOOLS as COMPUTER_TOOLS, call as local_computer_call
from agent.tools.github_tools import LOCAL_TOOLS as GITHUB_LOCAL_TOOLS
from sandbox.broker_transport import send_payload


LOCAL_TOOLS = frozenset({tool['name'] for tool in COMPUTER_TOOLS} | GITHUB_LOCAL_TOOLS | {
    'repository_guidance',
    'credentials_run', 'artifact_publish', 'artifact_fetch',
})


def requires_computer(name):
    return name in LOCAL_TOOLS or name.startswith('browser_')


def reference(arguments):
    """Only immutable public history is readable in the isolated control guest."""
    path = Path(arguments.get('path', ''))
    root = os.environ.get('MOYAI_HISTORY_REFERENCE_DIR')
    if (not root or not path.is_absolute() or path.parent != Path(root)
            or not re.fullmatch(r'\.moyai-history-[0-9a-f]{64}\.jsonl', path.name)):
        return None
    if path.is_symlink():
        raise ValueError('A public history reference cannot be a symbolic link.')
    return local_computer_call('computer_read_file', arguments, root)


def content(result, *, error=False):
    return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': error}


def call(name, arguments, broker):
    if name == 'computer_read_file':
        result = reference(arguments)
        if result is not None:
            return content(result)
    # A request identity is minted before submission. The owner durably records
    # dispatch/receipt; an HTTP retry cannot execute the same command twice.
    identity = uuid4().hex
    payload = {'action': 'computer_call', 'request_id': identity, 'name': name, 'arguments': arguments}
    accepted = False
    def send(route, body):
        nonlocal payload
        if route == '/agent-runtime':
            payload = body  # Reuse a verified upload manifest on later polls.
        return broker(route, body)
    while True:
        try:
            result = send_payload('/agent-runtime', payload, send)
        except HTTPError as exc:
            definite = not accepted and exc.code in {400, 401, 403, 404, 409, 413, 422}
            return content({'outcome': 'rejected' if definite else 'unknown', 'request_id': identity,
                'dispatched': False if definite else None, 'status_code': exc.code,
                'message': ('Computer operation was rejected before acceptance.' if definite else
                    'Inspect computer_operation_read with this request_id before retrying.')}, error=True)
        except (OSError, URLError):
            # A lost response must expose the identity needed to reconcile the
            # operation. It never starts a replacement write under a new ID.
            return content({'outcome': 'unknown', 'request_id': identity,
                'message': 'Computer operation could not be confirmed. Inspect this request with computer_operation_read before retrying.'}, error=True)
        if result['status'] in {'completed', 'unknown', 'cancelled', 'rejected'}:
            return result['result']
        accepted = True
        time.sleep(.25)
