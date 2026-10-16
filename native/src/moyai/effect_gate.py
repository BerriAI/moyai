"""Coordinate sibling effects within Temporal's existing workflow owner."""

from __future__ import annotations

import asyncio

from temporalio import workflow

EFFECT_GATE_VERSION = "moyai-sibling-effect-gate-v1"


def workflow_effect_lock(session_id: str) -> asyncio.Lock:
    """Share one lifecycle lock across all native runs in this workflow.

    Capability instances can be cloned for self-delegation and are also used by
    activities. The workflow instance is the common, replay-reconstructed owner;
    no worker-global lock or serialized dependency is involved. SQL continues to
    fence unresolved effects across workflow executions.
    """
    owner = workflow.instance()
    locks: dict[str, asyncio.Lock] | None = getattr(owner, "_moyai_effect_locks", None)
    if locks is None:
        locks = {}
        owner._moyai_effect_locks = locks
    if session_id not in locks:
        locks[session_id] = asyncio.Lock()
    return locks[session_id]
