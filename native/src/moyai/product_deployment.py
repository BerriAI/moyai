"""Deployment admission checks, using the existing product schema contract.

This is not a release controller. Production core transitions require the
existing maintenance/release workflow, provisioned schemas and role fences.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module, metadata

from .product_config import ProductConfig, Role

RUNTIME_VERSIONS = {
    "pydantic-ai": "2.55.0",
    "pydantic-ai-harness": "0.55.0",
    "openai": "3.29.0",
}


class ProductDeploymentError(RuntimeError):
    """A safe startup error without credentials or provider response bodies."""


@dataclass(frozen=True)
class RolePlan:
    role: Role
    serve_api: bool
    run_dispatcher: bool
    run_worker: bool
    blockers: tuple[str, ...] = ()


def build_role_plan(config: ProductConfig) -> RolePlan:
    """Describe reviewed ownership; schema/owner/drain checks still run at startup."""
    role = config.role
    blockers: tuple[str, ...] = ()
    if role not in {"api", "coordinator", "broker", "worker", "standalone"}:
        blockers = ("The native runtime role is not supported.",)
    elif config.product_mode == "existing":
        if role == "standalone" and not config.local_rehearsal:
            blockers = ("Existing product startup requires an explicit API/coordinator/broker/worker role.",)
    elif config.product_mode != "standalone" or role != "standalone":
        blockers = ("Standalone startup cannot own a distributed product role.",)
    return RolePlan(
        # Each distributed role has an HTTP health listener. ProductRoleMiddleware
        # limits business routes to the role that actually owns them.
        role=role, serve_api=True,
        run_dispatcher=role in {"coordinator", "standalone"},
        run_worker=role == "worker" or role == "standalone",
        blockers=blockers,
    )


def require_startable(config: ProductConfig) -> RolePlan:
    plan = build_role_plan(config)
    if plan.blockers:
        raise ProductDeploymentError(" ".join(plan.blockers))
    return plan


def check_runtime_dependencies() -> dict[str, str]:
    """Check the shared runtime lock, without importing any old agent engine."""
    installed: dict[str, str] = {}
    for name, expected in RUNTIME_VERSIONS.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ProductDeploymentError("Install Moyai using the repository lock.") from None
        if actual != expected:
            raise ProductDeploymentError(f"The native runtime requires locked {name}=={expected}.")
        installed[name] = actual
    try:
        metadata.version("openai-agents")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise ProductDeploymentError("Retired agent SDKs cannot be installed in the Moyai runtime.")
    try:
        for name in (
            "jwt", "psycopg", "psycopg_pool", "app.config", "app.security",
            "app.db", "app.blob_storage", "app.google_sso",
        ):
            import_module(name)
    except ImportError:
        raise ProductDeploymentError("Install the complete Moyai package, including its product services.") from None
    return installed


def verify_product_schema(config: ProductConfig) -> dict[str, int]:
    """Bounded, read-only check of the original product migration receipts."""
    if config.product_mode != "existing":
        raise ProductDeploymentError("Product schema verification requires existing mode.")
    import psycopg
    from app.database import DatabaseError
    from app.online_migrations import schema_support
    from app.schema_migrations import prepare_schema

    try:
        with psycopg.connect(config.database_url, connect_timeout=5) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '5s'")
            prepare_schema(connection, config.product_schema, "verify")
            if config.legacy_settings().moyai_worker_adoption_bridge:
                from .worker_adoption import verify_bridge_before_runtime
                verify_bridge_before_runtime(config, connection)
            return schema_support()
    except (psycopg.Error, DatabaseError, ValueError):
        raise ProductDeploymentError(
            "Product schema verification failed; use the existing reviewed migration/preflight procedure."
        ) from None


def candidate_role_fingerprint(config: ProductConfig) -> str:
    """Use the same core-aware contract as canonical role admission/release probes."""
    if config.product_mode != "existing":
        raise ProductDeploymentError("A product role contract requires existing mode.")
    from app.runtime_compatibility import api_fingerprint, background_fingerprint

    source = config.legacy_settings()
    return api_fingerprint(source) if config.role == "api" else background_fingerprint(source, config.role)
