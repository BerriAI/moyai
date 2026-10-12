"""Authenticated request envelopes for the sandbox's private broker channel.

Repository/issue text can contain shell or SQL examples. It is application data,
not a web request to execute. Seal it across the public edge and validate it only
after the broker authenticates the short-lived, run-scoped capability.
"""
import base64
import hashlib
import json
from itertools import count

from cryptography.fernet import Fernet, InvalidToken

CONTENT_TYPE = 'application/vnd.moyai.broker-v1'
MAX_BODY = 5 * 1024 * 1024
MAX_WIRE = 7 * 1024 * 1024


def scoped_tools(send, scope):
    """Bind nested calls before payload chunking; transport retries keep the ID."""
    if not scope:
        return send
    sequence = count()
    def invoke(path, body=None):
        if path == '/tools/call' and body is not None:
            identity = hashlib.sha256(f"{scope['request_id']}:{next(sequence)}".encode()).hexdigest()[:32]
            body = {**body, 'input_id': scope['input_id'], 'request_id': identity}
        return send(path, body)
    return invoke


def send_payload(path, body, send):
    """Send complete JSON; stable parts can be resent after acknowledgment loss."""
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) <= body_limit(path):
        return send(path, body)
    digest = hashlib.sha256(raw).hexdigest()
    parts = 0
    for offset in range(0, len(raw), MAX_BODY // 2):
        send('/payloads', {'route': path, 'digest': digest, 'part': parts,
                          'data': base64.b64encode(raw[offset:offset + MAX_BODY // 2]).decode()})
        parts += 1
    return send(path, {'moyai_payload': {'digest': digest, 'parts': parts},
                       **({'name': body['name']} if path == '/tools/call' else {})})


def body_limit(path):
    return MAX_BODY


def wire_limit(path):
    return MAX_WIRE


def cipher(token):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'moyai-broker-v1\0' + token.encode()).digest()))


def seal(token, path, body):
    if len(body) > body_limit(path):
        raise ValueError('Broker request is too large.')
    return cipher(token).encrypt(path.encode() + b'\n' + body)


def unseal(token, path, body):
    if len(body) > wire_limit(path):
        raise ValueError('Broker request is too large.')
    try:
        decoded = cipher(token).decrypt(body, ttl=300)
        target, payload = decoded.split(b'\n', 1)
    except (InvalidToken, ValueError):
        raise ValueError('Invalid or expired broker envelope.') from None
    if target != path.encode() or len(payload) > body_limit(path):
        raise ValueError('Invalid broker envelope destination or size.')
    return payload
