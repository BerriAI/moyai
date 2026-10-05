"""Authenticated request envelopes for the sandbox's private broker channel.

Repository/issue text can contain shell or SQL examples. It is application data,
not a web request to execute. Seal it across the public edge and validate it only
after the broker authenticates the short-lived, run-scoped capability.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

CONTENT_TYPE = 'application/vnd.moyai.broker-v1'
MAX_BODY = 5 * 1024 * 1024
MAX_WIRE = 7 * 1024 * 1024
try:
    from .github_limits import MAX_PUBLICATION_BODY
except ImportError:
    from github_limits import MAX_PUBLICATION_BODY


def body_limit(path):
    return MAX_PUBLICATION_BODY if path == '/tools/call' else MAX_BODY


def wire_limit(path):
    return 4 * ((body_limit(path) + 4096) // 3 + 1) if path == '/tools/call' else MAX_WIRE


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
