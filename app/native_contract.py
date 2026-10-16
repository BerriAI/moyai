"""Core compatibility declarations shared by release probes and both interpreters.

This module imports no agent framework. A legacy process retains its exact old
fingerprint; native participants add the reviewed core and storage contract.
"""
from __future__ import annotations

import hashlib
import re

CORE_IDENTITY = "pydantic-v1"
# Split activity queues and lazy workspace startup change recorded workflow commands.
NATIVE_PROTOCOL_REVISION = 2
FRAMEWORK_VERSIONS = {
    "pydantic-ai": "2.55.0",
    "pydantic-ai-harness": "0.55.0",
    "openai": "3.29.0",
}


def core_contract(settings):
    if getattr(settings, "moyai_core", "legacy") == "legacy":
        return None
    if settings.moyai_core != "pydantic":
        raise ValueError("Unsupported execution core.")
    schema = settings.moyai_native_schema
    queue = settings.moyai_native_task_queue
    key = settings.moyai_native_encryption_key
    prefix = settings.moyai_native_object_prefix
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema or "")
        or schema == settings.moyai_database_schema or not queue
        or queue in {settings.temporal_task_queue, settings.temporal_task_queue + "-agents-v1"}
        or not key or len(key) < 32 or not prefix
        or settings.moyai_native_key_format != "sha256-text-fernet-v1"
        or settings.moyai_native_object_format != "fernet-hmac-sha256-v1"
    ):
        raise ValueError("The native execution/storage contract is incomplete or incompatible.")
    return {
        "core": CORE_IDENTITY,
        "protocol": NATIVE_PROTOCOL_REVISION,
        "frameworks": dict(FRAMEWORK_VERSIONS),
        "schema": schema, "queue": queue,
        "key_format": settings.moyai_native_key_format,
        "key_digest": hashlib.sha256(key.encode()).hexdigest(),
        "object_format": settings.moyai_native_object_format,
        "object_prefix": prefix,
    }
