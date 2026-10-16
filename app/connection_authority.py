"""Credential refresh preserves authority; reconnects create a new authority.

The marker lives inside the existing encrypted credential envelope. Old rows
derive their initial marker from ciphertext, so no schema/backfill is required.
Only a compare-and-set refresh may retain it.
"""

import hashlib
import json
import secrets

from .connector_errors import ConnectorError
from .db import now

AUTHORITY = "_moyai_authority"


def authority(credentials, encrypted):
    return credentials.get(AUTHORITY) or hashlib.sha256(encrypted.encode()).hexdigest()


def new_credentials(credentials):
    return {**credentials, AUTHORITY: secrets.token_hex(32)}


def refresh_connection(store, security, provider, original, credentials):
    previous = json.loads(security.decrypt(original["encrypted"]))
    fresh = {**credentials, AUTHORITY: authority(previous, original["encrypted"])}
    changed = store.execute(
        """UPDATE connections SET encrypted=?,updated_at=?
           WHERE provider=? AND encrypted=?""",
        (security.encrypt(json.dumps(fresh)), now(), provider, original["encrypted"]),
    )
    if not changed:
        raise ConnectorError(
            "The connection changed while refreshing. No action was sent.",
            code="access_changed", dispatched=False,
        )
    return fresh


def save_if_unchanged(store, security, provider, credentials, label, expected):
    """A verified onboarding result cannot overwrite a concurrent revocation."""
    encrypted = security.encrypt(json.dumps(new_credentials(credentials)))
    if expected is None:
        changed = store.execute(
            "INSERT INTO connections VALUES(?,?,?,?) ON CONFLICT(provider) DO NOTHING",
            (provider, encrypted, label, now()),
        )
    else:
        changed = store.execute(
            "UPDATE connections SET encrypted=?,label=?,updated_at=? WHERE provider=? AND encrypted=?",
            (encrypted, label, now(), provider, expected),
        )
    if not changed:
        raise ConnectorError("The connection changed. Start again from Connections.", dispatched=False)


def set_policy(store, security, provider, enabled, read_only):
    with store.connect() as conn:
        conn.begin_write()
        row = conn.execute("SELECT * FROM connection_policies WHERE provider=?", (provider,)).fetchone()
        changed = not row or (bool(row["enabled"]), bool(row["read_only"])) != (enabled, read_only)
        if changed:
            credential = conn.execute("SELECT encrypted FROM connections WHERE provider=?", (provider,)).fetchone()
            if credential:
                value = json.loads(security.decrypt(credential["encrypted"]))
                conn.execute("UPDATE connections SET encrypted=?,updated_at=? WHERE provider=?",
                             (security.encrypt(json.dumps(new_credentials(value))), now(), provider))
        conn.execute(
            """INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)
               ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled,read_only=excluded.read_only""",
            (provider, enabled, read_only),
        )
