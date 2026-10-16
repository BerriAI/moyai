"""Explicit configuration boundary between the product and its execution core.

Loading configuration performs no I/O, schema creation, or secret generation.
Validation is not deployment readiness: role ownership and provisioned schemas
must still be checked by the deployment entrypoint.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, EnvSettingsSource, InitSettingsSource

if TYPE_CHECKING:
    from app.config import Settings as SourceSettings

CORE_IDENTITY = "pydantic-v1"
NATIVE_KEY_FORMAT = "sha256-text-fernet-v1"
NATIVE_OBJECT_FORMAT = "fernet-hmac-sha256-v1"
ProductMode = Literal["standalone", "existing"]
Role = Literal["standalone", "api", "coordinator", "broker", "worker"]


class ProductConfigurationError(ValueError):
    """Safe operator-facing error; never includes configuration values."""


def select_core(environment: Mapping[str, str] | None = None) -> Literal["pydantic"]:
    env = os.environ if environment is None else environment
    value = env.get("MOYAI_CORE", "pydantic").strip()
    if value != "pydantic":
        raise ProductConfigurationError("Only MOYAI_CORE=pydantic is supported; retired engines require an earlier image.")
    return value  # type: ignore[return-value]


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or not value.strip():
        raise ProductConfigurationError(f"{name} must be explicitly configured.")
    return value


def _source_settings(environment: Mapping[str, str]) -> SourceSettings:
    # Use the real product validators and aliases, but neither .env nor ambient
    # process variables may override the caller's isolated configuration.
    from app.config import Settings as LegacySettings

    snapshot = {key.lower(): value for key, value in environment.items()}

    class SnapshotSource(EnvSettingsSource):
        def _load_env_vars(self) -> Mapping[str, str | None]:
            return snapshot

    class SnapshotSettings(LegacySettings):
        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
        ):
            return (
                init_settings, SnapshotSource(settings_cls),
                InitSettingsSource(settings_cls, {}), InitSettingsSource(settings_cls, {}),
            )

    try:
        return cast("SourceSettings", cast(type[BaseSettings], SnapshotSettings)(_env_file=None))
    except (ValueError, TypeError) as error:
        # ValidationError.errors() includes raw inputs; never return or log it.
        names = (
            sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
            if isinstance(error, ValidationError) else []
        )
        suffix = f" ({', '.join(names)})" if names else ""
        raise ProductConfigurationError(f"Existing product configuration is invalid{suffix}.") from None


@dataclass(frozen=True)
class ProductConfig:
    """Construct with load_product_config(); contains no readiness assertion."""

    product_mode: ProductMode
    role: Role
    database_url: str = field(repr=False)
    product_schema: str | None
    native_schema: str | None
    native_schema_mode: Literal["bootstrap", "verify"]
    native_encryption_key: SecretStr = field(repr=False)
    native_task_queue: str
    native_key_format: str
    native_object_prefix: str
    native_object_format: str
    local_rehearsal: bool
    _source: SourceSettings | None = field(default=None, repr=False, compare=False)

    @property
    def core_identity(self) -> str:
        return CORE_IDENTITY

    def legacy_settings(self) -> SourceSettings:
        """Original product keys, aliases and schema; no legacy runtime import."""
        if self._source is None:
            raise ProductConfigurationError("Standalone mode has no existing product settings.")
        return self._source.model_copy(deep=True, update={
            "moyai_core": "pydantic",
            "moyai_native_schema": self.native_schema or "",
            "moyai_native_task_queue": self.native_task_queue,
            "moyai_native_encryption_key": self.native_encryption_key.get_secret_value(),
            "moyai_native_key_format": self.native_key_format,
            "moyai_native_object_prefix": self.native_object_prefix,
            "moyai_native_object_format": self.native_object_format,
        })

    def native_overrides(self) -> dict[str, Any]:
        """Keyword overrides for moyai.settings.Settings (not an env mutation).

        The source Settings stays available separately for product auth, storage,
        accounting and release contracts. Object prefix/format remain explicit
        config inputs for the product storage adapter.
        """
        result: dict[str, Any] = {
            "product_mode": self.product_mode,
            "database_url": self.database_url,
            "database_schema": self.native_schema or "",
            "native_schema_mode": self.native_schema_mode,
            "encryption_key": self.native_encryption_key,
            "temporal_queue": self.native_task_queue,
        }
        if source := self._source:
            result.update(
                data_dir=source.data_dir / "pydantic",
                temporal_address=source.temporal_address,
                temporal_namespace=source.temporal_namespace,
                temporal_api_key=SecretStr(source.temporal_api_key),
                temporal_tls=source.temporal_tls,
                database_pool_size=source.moyai_database_pool_size,
                temporal_worker_activities=source.temporal_worker_activities,
                temporal_workflow_cache_size=source.temporal_workflow_cache_size,
                temporal_dispatch_concurrency=source.temporal_dispatch_concurrency,
                temporal_dispatch_batch_size=source.temporal_dispatch_batch_size,
                public_url=source.public_url,
                secure_cookies=urlsplit(source.public_url).scheme == "https",
                gateway_url=source.litellm_api_base,
                gateway_key=SecretStr(source.litellm_api_key),
                default_model=source.resolve_model(),
                model_catalog=",".join(dict.fromkeys(
                    source.resolve_model(value) for value in source.allowed_models()
                )),
                artifact_bucket=source.object_storage_bucket,
                artifact_endpoint=source.object_storage_endpoint,
                artifact_region=source.object_storage_region,
                artifact_prefix=self.native_object_prefix,
                artifact_access_key_id=SecretStr(source.object_storage_access_key_id),
                artifact_secret_access_key=SecretStr(source.object_storage_secret_access_key),
                artifact_session_token=SecretStr(source.object_storage_session_token),
            )
        return result


def load_product_config(environment: Mapping[str, str] | None = None) -> ProductConfig:
    """One execution core; existing identity/storage stays explicit and fails closed."""
    env = dict(os.environ if environment is None else environment)
    select_core(env)
    mode = env.get("MOYAI_PRODUCT_MODE", "standalone")
    if mode not in {"standalone", "existing"}:
        raise ProductConfigurationError("MOYAI_PRODUCT_MODE must be standalone or existing.")
    role = env.get("MOYAI_RUNTIME_ROLE", "standalone")
    if role not in {"standalone", "api", "coordinator", "broker", "worker"}:
        raise ProductConfigurationError("MOYAI_RUNTIME_ROLE is invalid.")
    rehearsal = env.get("MOYAI_PRODUCT_LOCAL_REHEARSAL", "false")
    if rehearsal not in {"true", "false"}:
        raise ProductConfigurationError("MOYAI_PRODUCT_LOCAL_REHEARSAL must be true or false.")
    local_rehearsal = rehearsal == "true"
    if mode == "standalone":
        if local_rehearsal:
            raise ProductConfigurationError("Product rehearsal requires MOYAI_PRODUCT_MODE=existing.")
        if role != "standalone" or env.get("RENDER_SERVICE_ID") or env.get("RENDER_EXTERNAL_URL"):
            raise ProductConfigurationError(
                "Distributed/Render Pydantic services require MOYAI_PRODUCT_MODE=existing."
            )
        if (
            env.get("MOYAI_DATABASE_SCHEMA") or env.get("MOYAI_SCHEMA_MODE") == "verify"
            or (env.get("MOYAI_DATABASE_URL") and "MOYAI_PRODUCT_MODE" not in env)
        ):
            raise ProductConfigurationError(
                "Select an explicit product mode; existing product schema settings cannot bootstrap standalone."
            )
        return ProductConfig(
            product_mode="standalone", role="standalone",
            database_url=env.get("MOYAI_DATABASE_URL", ""), product_schema=None,
            native_schema=None, native_schema_mode="bootstrap",
            native_encryption_key=SecretStr(env.get("ENCRYPTION_KEY", "")),
            native_task_queue=env.get("TEMPORAL_TASK_QUEUE", "moyai-pydantic-v1"),
            native_key_format=NATIVE_KEY_FORMAT, native_object_prefix="moyai",
            native_object_format=NATIVE_OBJECT_FORMAT, local_rehearsal=False,
        )

    if role == "standalone" and not local_rehearsal:
        raise ProductConfigurationError("Existing mode requires an explicit API/coordinator/broker/worker role.")
    database_url = _required(env, "MOYAI_DATABASE_URL")
    try:
        parsed = urlsplit(database_url)
        if (
            parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
            or not parsed.path.strip("/") or parsed.fragment or any(c.isspace() for c in database_url)
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ProductConfigurationError("MOYAI_DATABASE_URL must select an existing PostgreSQL database.") from None
    product_schema = _required(env, "MOYAI_DATABASE_SCHEMA")
    native_schema = _required(env, "MOYAI_NATIVE_SCHEMA")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", native_schema) or native_schema in {
        product_schema, "public", "information_schema",
    } or native_schema.startswith("pg_"):
        raise ProductConfigurationError("MOYAI_NATIVE_SCHEMA must be a separate non-system PostgreSQL identifier.")
    if env.get("MOYAI_SCHEMA_MODE") != "verify":
        raise ProductConfigurationError("Existing mode requires MOYAI_SCHEMA_MODE=verify.")
    if env.get("MOYAI_NATIVE_SCHEMA_MODE", "verify") != "verify":
        raise ProductConfigurationError("Existing mode requires MOYAI_NATIVE_SCHEMA_MODE=verify.")
    if env.get("MOYAI_DATABASE_INITIALIZE", "").lower() != "false":
        raise ProductConfigurationError("Existing mode requires explicit MOYAI_DATABASE_INITIALIZE=false.")
    if env.get("TEMPORAL_ENABLED", "").lower() != "true":
        raise ProductConfigurationError("Existing mode requires TEMPORAL_ENABLED=true.")
    for name in ("MOYAI_SEPARATE_BROKER", "MOYAI_COORDINATOR_HANDOFF"):
        required = "false" if local_rehearsal and role == "standalone" else "true"
        if env.get(name, "").lower() != required:
            raise ProductConfigurationError(f"Existing mode requires {name}={required} for this role.")
    _required(env, "SESSION_SECRET")
    try:
        Fernet(_required(env, "ENCRYPTION_KEY").encode())
    except (ValueError, TypeError):
        raise ProductConfigurationError("ENCRYPTION_KEY must retain the original raw Fernet key.") from None
    native_key = _required(env, "MOYAI_NATIVE_ENCRYPTION_KEY")
    if len(native_key) < 32:
        raise ProductConfigurationError("MOYAI_NATIVE_ENCRYPTION_KEY must contain at least 32 characters.")
    if _required(env, "MOYAI_NATIVE_KEY_FORMAT") != NATIVE_KEY_FORMAT:
        raise ProductConfigurationError(f"MOYAI_NATIVE_KEY_FORMAT must be {NATIVE_KEY_FORMAT}.")
    if _required(env, "MOYAI_NATIVE_OBJECT_FORMAT") != NATIVE_OBJECT_FORMAT:
        raise ProductConfigurationError(f"MOYAI_NATIVE_OBJECT_FORMAT must be {NATIVE_OBJECT_FORMAT}.")
    prefix = _required(env, "MOYAI_NATIVE_OBJECT_PREFIX")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix) or len(prefix) > 200:
        raise ProductConfigurationError("MOYAI_NATIVE_OBJECT_PREFIX must be a safe nonempty object prefix.")
    for name in ("OBJECT_STORAGE_BUCKET", "OBJECT_STORAGE_PREFIX", "TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE"):
        _required(env, name)
    queue = _required(env, "MOYAI_NATIVE_TASK_QUEUE")
    legacy_queue = env.get("TEMPORAL_TASK_QUEUE", "moyai-sessions-v1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", queue) or queue in {
        legacy_queue, f"{legacy_queue}-agents-v1",
    }:
        raise ProductConfigurationError("MOYAI_NATIVE_TASK_QUEUE must be separate from both legacy queues.")
    source = _source_settings(env)
    try:
        origin = urlsplit(source.public_url)
        valid_origin = (
            origin.scheme in {"http", "https"} and bool(origin.hostname)
            and not origin.username and not origin.password and not origin.query and not origin.fragment
            and origin.path in {"", "/"} and not any(c.isspace() for c in source.public_url)
            and (origin.scheme == "https" or _loopback_endpoint(source.public_url))
        )
        _ = origin.port
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise ProductConfigurationError("The product public URL must be an HTTPS origin or loopback HTTP origin.")
    if local_rehearsal:
        if any(env.get(key) for key in (
            "RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_INSTANCE_ID",
        )):
            raise ProductConfigurationError("Local product rehearsal cannot run on Render.")
        endpoints = (
            database_url, "//" + source.temporal_address, source.public_url,
            source.object_storage_endpoint,
        )
        # libpq query parameters can override the apparent URL host. Local
        # rehearsal uses an unambiguous loopback DSN, without alternate routing.
        if urlsplit(database_url).query or any(not _loopback_endpoint(value) for value in endpoints):
            raise ProductConfigurationError(
                "Local product rehearsal requires loopback database, Temporal, public URL and object storage."
            )
    # Overlapping prefixes can have different object formats. Require an
    # independently named destination subtree rather than guessing compatibility.
    old_prefix = source.object_storage_prefix
    if prefix == old_prefix or prefix.startswith(old_prefix + "/") or old_prefix.startswith(prefix + "/"):
        raise ProductConfigurationError("Native and product object prefixes must be separate.")
    return ProductConfig(
        product_mode="existing", role=role,  # type: ignore[arg-type]
        database_url=database_url, product_schema=product_schema, native_schema=native_schema,
        native_schema_mode="verify", native_encryption_key=SecretStr(native_key),
        native_task_queue=queue, native_key_format=NATIVE_KEY_FORMAT,
        native_object_prefix=prefix, native_object_format=NATIVE_OBJECT_FORMAT,
        local_rehearsal=local_rehearsal, _source=source,
    )


def _loopback_endpoint(value: str) -> bool:
    try:
        host = urlsplit(value).hostname
        if host == "localhost":
            return True
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False
