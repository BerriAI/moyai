"""Read-only, source-owned release capabilities and connection budgeting.

These are configured process bounds, not observed production headroom or an
availability claim. A controller must separately reserve capacity for ingress,
operators, migrations, reconnect overlap, other clients and platform surge.
Changing resource construction requires reviewing this protocol and its bounds.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .product_config import ProductConfig

PROTOCOL = "moyai-release-capabilities-v1"


def capabilities(config: ProductConfig) -> dict:
    if config.product_mode != "existing" or config.role not in {"api", "broker", "coordinator", "worker"}:
        raise ValueError("Release capabilities require a distributed existing-product role.")
    from render_start import (
        API_GRACEFUL_SHUTDOWN_SECONDS,
        BROKER_GRACEFUL_SHUTDOWN_SECONDS,
        LISTENER_GRACE_SECONDS,
        ROLE_CONTROL_PROTOCOL,
    )

    from .fleet_registry import PROTOCOL as ADMISSION_PROTOCOL
    from .product_continuation_restore import RESTORE_CONCURRENCY
    from .product_start import _settings
    from .worker_adoption import CHECKPOINT_PROTOCOL
    from .worker_adoption import PROTOCOL as ADOPTION_BRIDGE_PROTOCOL
    from .worker_capacity import WORKER_SHUTDOWN_VERSION

    settings = _settings(config)
    source = config.legacy_settings()
    # Each of these pools is separately bounded. The Harness stores share one
    # pool; they do not each allocate another. Identity/history share the product
    # engine. The two restore locks use NullPool under the restorer semaphore.
    pools = {
        "canonical": source.moyai_database_pool_size,
        "native": settings.database_pool_size,
        "identity_history": source.moyai_database_pool_size,
        "harness": settings.database_pool_size,
    }
    dedicated = {
        "canonical_owner": 1,
        "workspace_restore_locks": RESTORE_CONCURRENCY,
        "coordinator_listener": int(config.role == "coordinator"),
        "broker_model_slots": int(config.role == "broker"),
    }
    return {
        "protocol": PROTOCOL, "core": config.core_identity, "role": config.role,
        "source_sha": source.moyai_build_sha, "local_rehearsal": config.local_rehearsal,
        "role_control": {"protocol": ROLE_CONTROL_PROTOCOL, "render_entrypoint": True},
        "generation_admission": {
            "protocol": ADMISSION_PROTOCOL, "durable_role_seals": True, "image_revision_fence": True,
            "durable_drain_receipts": True,
            "startup_seal_fence": True,
        },
        "worker": {
            "deployment": settings.worker_deployment, "build": settings.worker_build_id,
            "shutdown_protocol": WORKER_SHUTDOWN_VERSION,
            "shutdown_seconds": settings.worker_shutdown_seconds,
        },
        "worker_checkpoint_retirement": CHECKPOINT_PROTOCOL,
        "worker_adoption": {
            "protocol": ADOPTION_BRIDGE_PROTOCOL, "bridge": settings.worker_adoption_bridge,
            "initial_same_image_required": True, "unversioned_retirement_qualified": False,
        },
        "http": {
            "listener_overlap_seconds": LISTENER_GRACE_SECONDS if config.role != "worker" else 0,
            "shutdown_seconds": (
                API_GRACEFUL_SHUTDOWN_SECONDS if config.role == "api"
                else BROKER_GRACEFUL_SHUTDOWN_SECONDS if config.role == "broker" else 20
            ),
        },
        "connections": {
            "pools": pools, "dedicated": dedicated,
            "configured_bound": sum(pools.values()) + sum(dedicated.values()),
            "operator_and_platform_reserve_included": False,
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args(argv)
    if args.render:
        from render_start import configure_environment
        configure_environment()
    from .product_config import load_product_config
    config = load_product_config()
    if config is None:
        parser.error("Select the native execution core.")
    print(json.dumps(capabilities(config)))


if __name__ == "__main__":
    main()
