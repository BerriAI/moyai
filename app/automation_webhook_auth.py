"""Pure, shared verification contract for product automation receivers."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from .automation_sources import IDENTIFIER

MAX_BODY = 262144


def fresh(value: Any, *, milliseconds: bool = False) -> None:
    try:
        valid = abs(time.time() - int(value) / (1000 if milliseconds else 1)) <= (
            60 if milliseconds else 300
        )
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise HTTPException(401, "Webhook timestamp has expired.")


def verify_webhook(
    provider: str, secret: bytes, body: bytes | bytearray, headers: Mapping[str, str],
) -> tuple[str, dict[str, Any], str]:
    """Return authenticated delivery identity, payload and provider event header.

    Body-only signatures deliberately ignore unsigned delivery headers. This is
    the existing receiver contract, shared without importing either executor.
    """
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Webhook payload is too large.")
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()

    def equals(expected: str, actual: str) -> bool:
        return hmac.compare_digest(expected.encode(), actual.encode())

    signed_id = ""
    if provider == "github":
        valid = equals("sha256=" + digest, headers.get("x-hub-signature-256", ""))
    elif provider == "linear":
        valid = equals(digest, headers.get("linear-signature", ""))
    elif provider == "jira":
        valid = equals("sha256=" + digest, headers.get("x-hub-signature", ""))
    elif provider == "pagerduty":
        valid = any(equals("v1=" + digest, sig.strip()) for sig in headers.get("x-pagerduty-signature", "").split(","))
    elif provider == "gitlab":
        if secret.startswith(b"whsec_"):
            timestamp, signed_id = headers.get("webhook-timestamp", ""), headers.get("webhook-id", "")
            fresh(timestamp)
            try:
                key = base64.b64decode(secret[6:], validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(401, "Invalid signing configuration.") from None
            expected = "v1," + base64.b64encode(hmac.new(
                key, signed_id.encode() + b"." + timestamp.encode() + b"." + body, hashlib.sha256,
            ).digest()).decode()
            valid = any(equals(expected, sig) for sig in headers.get("webhook-signature", "").split())
            if not re.fullmatch(IDENTIFIER, signed_id):
                valid = False
        else:
            valid = hmac.compare_digest(secret, headers.get("x-gitlab-token", "").encode())
    elif headers.get("x-moyai-signature"):
        timestamp, signed_id = headers.get("x-moyai-timestamp", ""), headers.get("x-moyai-event-id", "")
        fresh(timestamp)
        expected = "sha256=" + hmac.new(
            secret, timestamp.encode() + b"." + signed_id.encode() + b"." + body, hashlib.sha256,
        ).hexdigest()
        valid = equals(expected, headers.get("x-moyai-signature", "")) and bool(re.fullmatch(IDENTIFIER, signed_id))
    else:
        supplied = headers.get("x-webhook-secret", "") or headers.get("authorization", "").removeprefix("Bearer ")
        valid = hmac.compare_digest(secret, supplied.encode())
        signed_id = headers.get("x-moyai-event-id", "")
        if signed_id and not re.fullmatch(IDENTIFIER, signed_id):
            raise HTTPException(400, "Invalid event identifier.")
    if not valid:
        raise HTTPException(401, "Invalid webhook signature or secret.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid webhook JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook must be a JSON object.")
    if provider == "linear":
        fresh(payload.get("webhookTimestamp"), milliseconds=True)
    stable = {key: value for key, value in payload.items() if provider != "linear" or key != "webhookTimestamp"}
    delivery = provider + ":" + (
        signed_id or hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    )
    return delivery, payload, headers.get("x-github-event", "")
