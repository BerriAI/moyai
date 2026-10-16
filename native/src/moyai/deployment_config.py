"""Persistent configuration for an isolated Docker installation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class InstallationError(RuntimeError):
    """An operator-facing error that contains no credential values."""


SECRET_NAMES = (
    "WORKSPACE_PASSWORD", "SESSION_SECRET", "ENCRYPTION_KEY", "MOYAI_NATIVE_ENCRYPTION_KEY",
)


def state_path(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("DATA_DIR", "/data")) / "installation.json"


def read_state(environment: Mapping[str, str]) -> dict | None:
    path = state_path(environment)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise InstallationError("Protect installation.json with mode 0600; do not use a symlink.")
    try:
        state = json.loads(path.read_text())
        if (
            state["version"] != 1 or not isinstance(state["scope"], str)
            or not isinstance(state["secrets"], dict)
            or any(not state["secrets"].get(name) for name in SECRET_NAMES)
            or state["phase"] not in {"prepared", "initialized", "failed", "upgrading"}
        ):
            raise ValueError
        return state
    except (ValueError, KeyError, TypeError):
        raise InstallationError("The installation state is invalid; restore its protected backup.") from None


def write_state(environment: Mapping[str, str], state: dict) -> None:
    path = state_path(environment)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".installation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def standalone_lock(environment: Mapping[str, str]):
    import fcntl

    path = state_path(environment).parent / ".standalone-runtime.lock"
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallationError("The standalone application is still running; stop it before maintenance.") from None
        yield


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise InstallationError(f"Configure {name} in the installation's .env file.")
    return value


def _public_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost"
        if parsed.hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        if (
            not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not (parsed.scheme == "https" or parsed.scheme == "http" and loopback)
        ):
            raise ValueError
        _ = parsed.port
        return value.rstrip("/")
    except ValueError:
        raise InstallationError("MOYAI_PUBLIC_URL must be an HTTPS origin, or loopback HTTP for local tests.") from None


def build_revision(environment: Mapping[str, str]) -> str:
    embedded = Path("/app/build-revision")
    revision = embedded.read_text().strip() if embedded.is_file() else environment.get("MOYAI_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise InstallationError("Use a release image built with its full MOYAI_BUILD_SHA.")
    supplied = environment.get("MOYAI_BUILD_SHA")
    if supplied and supplied != revision:
        raise InstallationError("MOYAI_BUILD_SHA differs from the image's embedded revision.")
    return revision


def configuration(environment: Mapping[str, str], state: dict | None, *, role: str = "coordinator") -> dict[str, str]:
    values = dict(environment)
    values.setdefault("DATA_DIR", "/data")
    topology = values.get("MOYAI_INSTALL_TOPOLOGY", "standalone")
    if topology not in {"standalone", "split"}:
        raise InstallationError("MOYAI_INSTALL_TOPOLOGY must be standalone or split.")
    name = values.get("MOYAI_INSTALLATION_ID", "moyai")
    if not re.fullmatch(r"[a-z][a-z0-9]{0,7}", name):
        raise InstallationError("MOYAI_INSTALLATION_ID must have 1–8 lowercase letters/digits, starting with a letter.")
    database = _required(values, "MOYAI_DATABASE_URL")
    try:
        address = urlsplit(database)
        if (
            address.scheme not in {"postgres", "postgresql"} or not address.hostname or not address.path.strip("/")
            or address.fragment or any(character.isspace() for character in database)
        ):
            raise ValueError
        _ = address.port
    except ValueError:
        raise InstallationError("MOYAI_DATABASE_URL must select a PostgreSQL database.") from None
    for field in (
        "OBJECT_STORAGE_BUCKET", "LITELLM_API_BASE", "LITELLM_API_KEY", "AGENT_MODEL",
        "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
    ):
        _required(values, field)
    if values.get("SANDBOX_PROVIDER", "modal") != "modal":
        raise InstallationError("This Docker package currently supports Modal computers.")
    public_url = _public_origin(_required(values, "MOYAI_PUBLIC_URL"))
    temporal_mode = values.get("MOYAI_TEMPORAL_MODE", "external")
    if temporal_mode not in {"external", "bundled"}:
        raise InstallationError("MOYAI_TEMPORAL_MODE must be external or bundled.")
    namespace = values.get("TEMPORAL_NAMESPACE", "")
    if temporal_mode == "bundled":
        namespace = namespace or f"{name}-moyai"
        values.update(TEMPORAL_ADDRESS="temporal:7233", TEMPORAL_TLS="false", TEMPORAL_API_KEY="")
    else:
        _required(values, "TEMPORAL_ADDRESS")
        namespace = _required(values, "TEMPORAL_NAMESPACE")
    product_schema = "moyai" if name == "moyai" else f"moyai_{name}"
    native_schema = product_schema + "_n"
    original_prefix = values.get("OBJECT_STORAGE_PREFIX", "").strip("/") or f"{name}/product"
    native_prefix = values.get("MOYAI_NATIVE_OBJECT_PREFIX", "").strip("/") or f"{name}/native"
    if (
        original_prefix == native_prefix or original_prefix.startswith(native_prefix + "/")
        or native_prefix.startswith(original_prefix + "/")
    ):
        raise InstallationError("Product and native object prefixes must be separate.")
    scope = {
        "topology": topology, "installation": name, "database": address._replace(
            netloc=f"{address.hostname}:{address.port or 5432}", fragment="",
        ).geturl(),
        "product_schema": product_schema, "native_schema": native_schema,
        "bucket": values["OBJECT_STORAGE_BUCKET"], "endpoint": values.get("OBJECT_STORAGE_ENDPOINT", ""),
        "region": values.get("OBJECT_STORAGE_REGION", "us-east-1"),
        "product_prefix": original_prefix, "native_prefix": native_prefix,
        "temporal_address": values["TEMPORAL_ADDRESS"], "temporal_namespace": namespace,
        "public_url": public_url,
    }
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    if state is not None:
        if state["scope"] != digest:
            raise InstallationError("The installation's data destination or topology changed; use an explicit migration.")
        for field in SECRET_NAMES:
            supplied = values.get(field)
            if supplied and supplied != state["secrets"][field]:
                raise InstallationError(f"{field} differs from the persisted installation key; restore the original value.")
        values.update(state["secrets"])
    values.update(
        MOYAI_CORE="pydantic",
        MOYAI_PRODUCT_MODE="existing" if topology == "split" else "standalone",
        MOYAI_RUNTIME_ROLE=role if topology == "split" else "standalone",
        MOYAI_NATIVE_SCHEMA_MODE="verify",
        MOYAI_DATABASE_INITIALIZE="false",
        MOYAI_NATIVE_DATABASE_SCHEMA=native_schema,
        MOYAI_PUBLIC_URL=public_url,
        MOYAI_SECURE_COOKIES=str(public_url.startswith("https://")).lower(),
        MOYAI_BUILD_SHA=build_revision(values),
        MOYAI_WORKER_SHUTDOWN_SECONDS="240",
        MOYAI_HTTP_SHUTDOWN_SECONDS="270",
        TEMPORAL_ENABLED="true",
        TEMPORAL_TLS=values.get("TEMPORAL_TLS", "true"),
        TEMPORAL_NAMESPACE=namespace,
        TEMPORAL_TASK_QUEUE=f"{name}-original-v1" if topology == "split" else f"{name}-native-v1",
        OBJECT_STORAGE_PREFIX=original_prefix,
        MOYAI_NATIVE_OBJECT_PREFIX=native_prefix,
        MOYAI_ARTIFACT_BUCKET=values["OBJECT_STORAGE_BUCKET"],
        MOYAI_ARTIFACT_ENDPOINT=values.get("OBJECT_STORAGE_ENDPOINT", ""),
        MOYAI_ARTIFACT_REGION=values.get("OBJECT_STORAGE_REGION", "us-east-1"),
        MOYAI_NATIVE_ARTIFACT_PREFIX=native_prefix,
        MOYAI_NATIVE_TASK_QUEUE=f"{name}-native-v1",
        MOYAI_NATIVE_KEY_FORMAT="sha256-text-fernet-v1",
        MOYAI_NATIVE_OBJECT_FORMAT="fernet-hmac-sha256-v1",
        MOYAI_MODAL_APP=values.get("MODAL_APP_NAME") or f"{name}-moyai",
        PASSWORD_LOGIN_ENABLED="true",
        GOOGLE_ALLOWED_DOMAINS=values.get("GOOGLE_ALLOWED_DOMAINS", ""),
        GOOGLE_ADMIN_EMAILS=values.get("GOOGLE_ADMIN_EMAILS", ""),
        SESSION_TITLES_ENABLED=values.get("SESSION_TITLES_ENABLED", "false"),
        MAINTENANCE_DRAIN="false",
        MOYAI_INSTALL_SCOPE=digest,
    )
    if topology == "split":
        values.update(
            MOYAI_DATABASE_SCHEMA=product_schema, MOYAI_NATIVE_SCHEMA=native_schema,
            MOYAI_SCHEMA_MODE="verify", MOYAI_SEPARATE_BROKER="true", MOYAI_COORDINATOR_HANDOFF="true",
        )
    else:
        for field in ("MOYAI_DATABASE_SCHEMA", "MOYAI_NATIVE_SCHEMA", "MOYAI_SCHEMA_MODE"):
            values.pop(field, None)
        values.update(MOYAI_SEPARATE_BROKER="false", MOYAI_COORDINATOR_HANDOFF="false")
    if values.get("MOYAI_PRODUCT_LOCAL_REHEARSAL", "false") != "false":
        raise InstallationError("The deployment package does not enable local-rehearsal bypasses.")
    return values


def new_state(environment: Mapping[str, str], values: Mapping[str, str]) -> dict:
    generated = {
        "WORKSPACE_PASSWORD": secrets.token_urlsafe(24),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "MOYAI_NATIVE_ENCRYPTION_KEY": secrets.token_urlsafe(48),
    }
    for name in SECRET_NAMES:
        if environment.get(name):
            generated[name] = environment[name]
    if len(generated["WORKSPACE_PASSWORD"]) < 16:
        raise InstallationError("WORKSPACE_PASSWORD must contain at least 16 characters.")
    if min(len(generated[name]) for name in SECRET_NAMES[1:]) < 32:
        raise InstallationError("Supply session/encryption keys with at least 32 characters.")
    try:
        Fernet(generated["ENCRYPTION_KEY"].encode())
    except (ValueError, TypeError):
        raise InstallationError("ENCRYPTION_KEY must be a Fernet key.") from None
    return {
        "version": 1, "scope": values["MOYAI_INSTALL_SCOPE"], "secrets": generated,
        "phase": "prepared", "revision": values["MOYAI_BUILD_SHA"], "activation_pending": True,
        "standalone_lock_protocol": 1,
    }
