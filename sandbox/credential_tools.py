"""Approved access for one sandbox command; values never become MCP results."""
import base64
import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import ExitStack
from typing import Callable
from urllib.parse import quote

try:
    from .install_access_tools import ensure_tools
except ImportError:
    from install_access_tools import ensure_tools

MAX_OUTPUT = 128 * 1024


def redact(text: str, secrets: list[str]) -> str:
    # Decode provider JSON before scrubbing, including escaped values and keys.
    for value in sorted(set(secrets), key=len, reverse=True):
        if not value:
            continue
        variants = {value, json.dumps(value)[1:-1], quote(value, safe=''),
                    base64.b64encode(value.encode()).decode()}
        for variant in sorted(variants, key=len, reverse=True):
            text = text.replace(variant, '[credential redacted]')
            # A bounded read can end in the middle of a long credential.
            start = text.rfind(variant[:min(8, len(variant))])
            if start >= 0 and variant.startswith(text[start:]):
                text = text[:start] + '[credential redacted]'
    return text


def failure_kind(output: str) -> str:
    # Only recognizable authentication errors trigger replacement. A nonzero
    # exit, network error, 403, or quota failure does not prove an expired key.
    if re.search(r'(?i)ExpiredToken(?:Exception)?|\btoken (?:has |is )?expired\b|\bcredentials? (?:have |has |are |is )?expired\b', output):
        return 'expired'
    if re.search(r'(?i)InvalidClientTokenId|UnrecognizedClientException|\binvalid[_ ](?:api[_ ]key|token)\b|\bUnauthorized\b', output):
        return 'invalid'
    if re.search(r'(?i)AccessDenied(?:Exception)?|\bForbidden\b|insufficient_scope|\bpermission denied\b', output):
        return 'permission'
    return 'unknown'


def execute(command: str, env: dict[str, str], fds: list[int], timeout: int) -> tuple[int, str, bool]:
    """Bound both memory and process lifetime, including background children."""
    output = bytearray()
    timed_out = False
    with subprocess.Popen(['/bin/bash', '-c', command], env=env, pass_fds=tuple(fds),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        break
                if not selector.get_map() and process.poll() is None:
                    try:
                        process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            # A shell may exit while its descendants still hold the credential.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    text = bytes(output).decode('utf-8', errors='replace')
    return process.returncode, text, timed_out


def run(arguments: dict, broker: Callable) -> dict:
    request_ids = arguments.get('request_ids')
    command, timeout = arguments.get('command'), arguments.get('timeout', 120)
    if (set(arguments) - {'request_ids', 'command', 'timeout'} or not isinstance(request_ids, list)
            or not 1 <= len(request_ids) <= 8
            or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{32}', item) for item in request_ids)
            or len(set(request_ids)) != len(request_ids)
            or not isinstance(command, str) or not command.strip() or len(command) > 16000
            or isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600):
        return {'error': 'Invalid credential command. Supply request_ids, command, and a timeout of 1–600 seconds.'}
    try:
        ensure_tools(command)
    except Exception:
        return {'error': 'An infrastructure CLI could not be installed. No credential was loaded or command executed.'}
    material = broker('/credentials/materialize', {'request_ids': request_ids})
    if material.get('status') != 'ready':
        return material
    bindings = material['bindings']
    secrets: list[str] = []
    fds: list[int] = []
    additions: dict[str, str] = {}
    try:
        with ExitStack() as stack:
            for binding in bindings:
                value = binding['value']
                secrets.append(value)
                if binding['format'] == 'env':
                    fields = json.loads(value)
                    secrets.extend(fields.values())
                else:
                    if not hasattr(os, 'memfd_create'):
                        return {'error': 'Credential files require the Linux sandbox runtime. No command was executed.'}
                    fd = os.memfd_create('moyai-credential', os.MFD_CLOEXEC)
                    stack.callback(os.close, fd)
                    os.fchmod(fd, 0o600)
                    os.write(fd, value.encode())
                    os.lseek(fd, 0, os.SEEK_SET)
                    fds.append(fd)
                    fields = {binding['env_var']: f'/proc/self/fd/{fd}'}
                    # Common structured file credentials may echo individual
                    # tokens rather than the complete kubeconfig/JSON document.
                    try:
                        def leaves(item: object) -> list[str]:
                            if isinstance(item, dict):
                                return [text for child in item.values() for text in leaves(child)]
                            if isinstance(item, list):
                                return [text for child in item for text in leaves(child)]
                            return [item] if isinstance(item, str) and len(item) >= 8 else []
                        secrets.extend(leaves(json.loads(value)))
                    except ValueError:
                        pass
                    secrets.extend(match.group(1).strip(' \"\'') for match in
                                   re.finditer(r'(?im)^\s*(?:token|password|client-key-data|client-certificate-data|private_key):\s*(.+)$', value))
                if additions.keys() & fields.keys():
                    return {'error': 'The selected credentials define the same environment variable. Choose one binding per variable.'}
                additions.update(fields)
            # Platform capabilities must not accidentally become CLI credentials.
            token = os.environ.get('WORKSPACE_RUN_TOKEN')
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('WORKSPACE_', 'MOYAI_')) and (not token or value != token)}
            code, output, timed_out = execute(command, {**env, **additions}, fds, timeout)
            clean = redact(output, secrets)
            limited = len(output.encode()) > MAX_OUTPUT
        result = {'exit_code': code, 'output': clean[:MAX_OUTPUT], 'timed_out': timed_out,
                  'output_limited': limited,
                  'credentials': [{'request_id': b['request_id'], 'revision': b['revision']} for b in bindings]}
        kind = failure_kind(output) if code and not timed_out else 'unknown'
        if code:
            result['failure'] = kind
            result['instruction'] = 'Inspect the result before retrying; a command may have partially completed.'
        source_binding = any(binding.get('name') == '1password-shared' for binding in bindings)
        if kind in {'expired', 'invalid', 'permission'} and source_binding:
            # An op-run child may reject a key fetched from Shared while the
            # service-account token remains valid. Do not revoke the source.
            result['instruction'] += (' This command used a 1Password source. Determine whether the error came '
                'from 1Password or the destination service. Report a source credential failure only after '
                'confirming its own authentication failed; otherwise report the source_checks outcome '
                'for the requested service. Keep vault values out of output.')
        elif kind in {'expired', 'invalid', 'permission'} and len(bindings) == 1:
            # Multiple bindings cannot safely attribute which credential failed.
            binding = bindings[0]
            report = broker('/tools/call', {'name': 'credentials_report_failure', 'arguments': {
                'request_id': binding['request_id'], 'revision': binding['revision'], 'failure': kind}})
            result['access'] = report
            if report.get('moyai_wait_credential'):
                result['moyai_wait_credential'] = report['moyai_wait_credential']
        return result
    except Exception:
        return {'error': 'The credential command could not be confirmed. Inspect the destination before retrying.'}
    finally:
        for binding in bindings:
            binding['value'] = ''
        additions.clear()
        secrets.clear()
