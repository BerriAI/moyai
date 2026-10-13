"""Reviewed API-to-cluster contract, owned by the release source, never by env.

Bump API_PROTOCOL_REVISION for incompatible changes to API durable writes,
coordinator inboxes, execution state, broker capabilities or signed sessions.
Equal revisions declare compatibility; they do not establish it automatically.
See docs/api-replicas.md for the review and cross-release rehearsal requirements.
"""
import hashlib
import json

from .database import RUNTIME_SETTINGS
from .schema_migrations import SCHEMA_COMPATIBILITY_REVISION

# Unlimited swarms persist budget_seconds=0 and ends_at=''. Older roles parse
# every deadline as a timestamp and cannot safely read or execute these rows.
# Automation startup now references the broker's complete automation_history tool.
# Scoped automation definitions, grants and attributed inputs require all roles
# to understand self-management and immutable owner-authored workflow sources.
# Public progress is complete and requested goal checkpoints have durable
# reporting schedules/receipts; old roles can discard or omit their delivery.
# Side-chat snapshots retain complete attributed inputs; workers compose large
# instruction sources as immutable references using the selected model window.
# Immutable attachment manifests/parts must be understood by intake and workers.
# Publishing workflow files uses explicit per-operation Workflows permission.
# Snapshotless workers wait for shared preparation; restored services are verified.
# GitHub coding capabilities and shared environment-template publication policy.
# The default Deep Agents engine, separate activity queues and encrypted payload
# codec require a coordinated upgrade beyond the native protocol-17 release.
# Independent control guests can now be warm. Older workers would resume these
# as full workspace computers, so adoption/rollback requires matching roles.
# Native SDK sessions add a persisted engine ID, full graph checkpoints and
# native activity queues. All roles must understand them before admitting work.
API_PROTOCOL_REVISION = 19
# Bump for incompatible durable session state, Temporal workflow histories or
# activity payloads, sandbox journals/capabilities, and worker/broker writes.
# Matching revisions require a reviewed cross-release handoff and rollback.
WORKER_PROTOCOL_REVISION = 19
BROKER_PROTOCOL_REVISION = 19
COORDINATOR_PROTOCOL_REVISION = 19


def api_fingerprint(settings):
    return hashlib.sha256(json.dumps({
        'protocol': API_PROTOCOL_REVISION,
        'schema': SCHEMA_COMPATIBILITY_REVISION,
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
        'schema': SCHEMA_COMPATIBILITY_REVISION,
        'settings': {name: getattr(settings, name) for name in RUNTIME_SETTINGS if name != 'moyai_build_sha'},
    }, sort_keys=True).encode()).hexdigest()


def coordinator_fingerprint(settings):
    return background_fingerprint(settings, 'coordinator')
