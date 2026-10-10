"""Reviewed API-to-cluster contract, owned by the release source, never by env.

Bump API_PROTOCOL_REVISION for incompatible changes to API durable writes,
coordinator inboxes, execution state, broker capabilities or signed sessions.
Equal revisions declare compatibility; they do not establish it automatically.
See docs/api-replicas.md for the review and cross-release rehearsal requirements.
"""
import hashlib
import json

from .database import RUNTIME_SETTINGS
from .schema_migrations import SCHEMA_REVISION

API_PROTOCOL_REVISION = 1
# Bump for incompatible durable session state, Temporal workflow histories or
# activity payloads, sandbox journals/capabilities, and worker/broker writes.
# Matching revisions require a reviewed cross-release handoff and rollback.
WORKER_PROTOCOL_REVISION = 1
BROKER_PROTOCOL_REVISION = 1
COORDINATOR_PROTOCOL_REVISION = 1


def api_fingerprint(settings):
    return hashlib.sha256(json.dumps({
        'protocol': API_PROTOCOL_REVISION,
        'schema': SCHEMA_REVISION,
        'settings': {name: getattr(settings, name) for name in RUNTIME_SETTINGS if name != 'moyai_build_sha'},
    }, sort_keys=True).encode()).hexdigest()


def worker_fingerprint(settings):
    return background_fingerprint(settings, 'worker')


def broker_fingerprint(settings):
    return background_fingerprint(settings, 'broker')


def background_fingerprint(settings, role):
    """Conservative shared declaration for mutually dependent background roles."""
    return hashlib.sha256(json.dumps({
        'role': role,
        'worker_protocol': WORKER_PROTOCOL_REVISION,
        'broker_protocol': BROKER_PROTOCOL_REVISION,
        'coordinator_protocol': COORDINATOR_PROTOCOL_REVISION,
        'api_protocol': API_PROTOCOL_REVISION,
        'schema': SCHEMA_REVISION,
        'settings': {name: getattr(settings, name) for name in RUNTIME_SETTINGS if name != 'moyai_build_sha'},
    }, sort_keys=True).encode()).hexdigest()
