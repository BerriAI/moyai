"""Run one admitted Kubernetes role from an immutable installation snapshot.

Fleet provisioning, compatibility, traffic routing and retirement belong to the
release controller. This entrypoint never initializes a database, changes the
maintenance generation, or promotes a Temporal worker version.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import cast

from pydantic_settings import BaseSettings

from .deployment_config import InstallationError, configuration, read_state

ROLES = ("api", "broker", "coordinator", "worker")
CONTROL_SOCKET = "/tmp/moyai-release/control.sock"


def qualification(role: str) -> dict:
    """Describe this image's contracts without starting clients or migrating."""
    from app.config import Settings
    from app.runtime_compatibility import api_fingerprint, background_fingerprint
    from app.schema_migrations import SCHEMA_REVISION

    from .deployment_capabilities import capabilities
    from .product_config import load_product_config
    from .product_deployment import check_runtime_dependencies, require_startable
    from .product_schema import MIGRATIONS, _manifest, application_metadata

    config = load_product_config()
    if config is None or config.role != role or config.native_schema is None:
        raise InstallationError("The qualification role does not match the configured role.")
    require_startable(config)
    versions = check_runtime_dependencies()
    settings = cast(Settings, cast(type[BaseSettings], Settings)(_env_file=None))
    return {
        "capabilities": capabilities(config),
        "contract": {
            "schema": SCHEMA_REVISION,
            "contracts": {
                "api": api_fingerprint(settings),
                **{name: background_fingerprint(settings, name) for name in ROLES if name != "api"},
            },
        },
        "manifest": {
            "schema": config.native_schema,
            "app": {
                version: _manifest(application_metadata(config.native_schema, version=version))
                for version, _ in MIGRATIONS
            },
            "harness": {
                "sdk_version": versions["pydantic-ai"],
                "harness_version": versions["pydantic-ai-harness"],
            },
        },
    }


def environment(role: str) -> dict[str, str]:
    if role not in ROLES:
        raise InstallationError("Select an explicit Kubernetes application role.")
    original = dict(os.environ)
    if (
        original.get("MOYAI_FLEET_PROVIDER") != "kubernetes"
        or original.get("MOYAI_FLEET_REQUIRED") != "true"
        or original.get("MOYAI_INSTALL_TOPOLOGY") != "split"
        or original.get("MOYAI_COLOCATED_ROLES", "false") != "false"
        or original.get("MAINTENANCE_DRAIN", "false") != "false"
        or not original.get("MOYAI_RELEASE_GENERATION")
        or not original.get("MOYAI_WORKER_DEPLOYMENT")
        or not original.get("MOYAI_WORKER_BUILD_ID")
    ):
        raise InstallationError("Kubernetes fleets require registered, versioned, independent roles.")
    state = read_state(original)
    if (
        state is None or state["phase"] != "initialized"
        or state.get("activation_pending") is not False or state.get("upgrade") is not None
    ):
        raise InstallationError("Complete installation activation or maintenance before preparing a fleet.")
    values = configuration(original, state, role=role)
    # Each candidate retains the original keys and data scope. Its immutable
    # image is admitted separately by FleetAdmission; a file revision is not a
    # compatibility assertion and never authorizes joining an unknown fleet.
    values["MOYAI_COLOCATED_ROLES"] = "false"
    values["MOYAI_ROLE_CONTROL_SOCKET"] = CONTROL_SOCKET
    return values


def prepare(snapshot: Path) -> None:
    """Copy a projected Secret into a private, process-local directory.

    Kubernetes projected volumes use symlinks; normal installation reads reject
    symlinks and writable shared state. Copy once with exclusive creation. Never
    overwrite the state of an already initialized pod.
    """
    import stat

    from .deployment_config import state_path

    if not snapshot.is_absolute():
        raise InstallationError("The installation snapshot must have an absolute path.")
    raw = snapshot.read_bytes()
    if len(raw) > 65536:
        raise InstallationError("The installation snapshot exceeds its size limit.")
    try:
        json.loads(raw)
    except (ValueError, UnicodeError):
        raise InstallationError("The installation snapshot is invalid.") from None
    target = state_path(os.environ)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise InstallationError("The private runtime directory has unsafe permissions.")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        state = read_state(os.environ)
        if (
            state is None or state["phase"] != "initialized"
            or state.get("activation_pending") is not False or state.get("upgrade") is not None
        ):
            raise InstallationError("The snapshot must describe a completed installation.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser("prepare")
    copy.add_argument("--snapshot", required=True, type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("role", choices=ROLES)
    serve.add_argument("--port", type=int, default=8787)
    check = commands.add_parser("check")
    check.add_argument("role", choices=ROLES)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.snapshot)
            return
        os.environ.update(environment(args.role))
        if args.command == "check":
            print(json.dumps(qualification(args.role)))
            return
        from .product_start import main as start

        start([
            "serve", "--host", "0.0.0.0", "--port", str(args.port),
            "--control-socket", CONTROL_SOCKET,
        ])
    except Exception as error:  # noqa: BLE001 - redact credential-bearing settings errors at the CLI boundary
        # Settings validation may embed input values. Never expose configuration
        # or installation keys in an init-container/qualification Job log.
        parser.exit(1, f"{type(error).__name__}: Kubernetes fleet startup failed.\n")


if __name__ == "__main__":
    main()
