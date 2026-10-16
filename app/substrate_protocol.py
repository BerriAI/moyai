"""Signed, actor-bound requests. Only the Moyai server holds the private key."""
import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

VERSION = 1
MAX_BODY = 2 * 1024 * 1024
CHUNK = 1024 * 1024


def canonical(actor, timestamp, nonce, path, body):
    return '\n'.join((actor, timestamp, nonce, path, hashlib.sha256(body).hexdigest())).encode()


def private_key(value):
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(value, validate=True))


def public_key(value):
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(value, validate=True))
