"""User authentication and encrypted application records; no agent-owned authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use a password of at least 12 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt:{salt}:{digest}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split(":")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class Security:
    def __init__(self, key: bytes):
        self.cipher = Fernet(key)
        self.signer = URLSafeTimedSerializer(key, salt="moyai-session-v1")
        self.object_key = hmac.digest(key, b"moyai-object-identity-v1", "sha256")

    def object_id(self, data: bytes) -> str:
        """Stable private references without exposing plaintext content hashes."""
        return hmac.digest(self.object_key, data, "sha256").hex()

    def seal(self, value: bytes | str | dict[str, Any] | list[Any]) -> str:
        if isinstance(value, str):
            raw = value.encode()
        elif isinstance(value, bytes):
            raw = value
        else:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return self.cipher.encrypt(raw).decode()

    def unseal(self, value: str) -> bytes:
        return self.cipher.decrypt(value.encode())

    def issue_session(self, user_id: str, version: int) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        token = self.signer.dumps({"user": user_id, "version": version, "csrf": csrf})
        return token, csrf

    def read_session(self, token: str) -> dict[str, Any] | None:
        try:
            value = self.signer.loads(token, max_age=7 * 86400)
            if not isinstance(value, dict) or not all(key in value for key in ("user", "version", "csrf")):
                return None
            return value
        except (BadSignature, SignatureExpired):
            return None

