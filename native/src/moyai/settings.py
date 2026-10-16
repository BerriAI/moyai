"""Application configuration. Credentials stay on the trusted API/worker hosts."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import tempfile
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True,
    )

    data_dir: Path = Field(default=Path(".data"), validation_alias="DATA_DIR")
    database_url: str = Field(default="", validation_alias="MOYAI_DATABASE_URL")
    product_mode: Literal["standalone", "existing"] = Field(
        default="standalone", validation_alias="MOYAI_PRODUCT_MODE",
    )
    database_schema: str = Field(
        default="", pattern=r"^(?:[a-z][a-z0-9_]{0,62})?$",
        validation_alias="MOYAI_NATIVE_DATABASE_SCHEMA",
    )
    native_schema_mode: Literal["auto", "bootstrap", "verify"] = Field(
        default="auto", validation_alias="MOYAI_NATIVE_SCHEMA_MODE",
    )
    encryption_key: SecretStr = Field(default=SecretStr(""), validation_alias="ENCRYPTION_KEY")
    workspace_password: SecretStr = Field(default=SecretStr(""), validation_alias="WORKSPACE_PASSWORD")
    administrator: str = Field(default="admin", validation_alias="MOYAI_ADMIN_USER")
    gateway_url: str = Field(
        default="https://gateway-dev.litellm-sandbox.ai/v1", validation_alias="LITELLM_API_BASE"
    )
    gateway_key: SecretStr = Field(default=SecretStr(""), validation_alias="LITELLM_API_KEY")
    default_model: str = Field(
        default="openai/gpt-6-astra", validation_alias=AliasChoices("MOYAI_MODEL", "AGENT_MODEL")
    )
    model_catalog: str = Field(default="", validation_alias="MOYAI_MODELS")
    temporal_address: str = Field(default="127.0.0.1:7233", validation_alias="TEMPORAL_ADDRESS")
    temporal_namespace: str = Field(default="default", validation_alias="TEMPORAL_NAMESPACE")
    temporal_queue: str = Field(default="moyai-pydantic-v1", validation_alias="TEMPORAL_TASK_QUEUE")
    temporal_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEMPORAL_API_KEY")
    temporal_tls: bool = Field(default=False, validation_alias="TEMPORAL_TLS")
    worker_shutdown_seconds: int = Field(
        default=0, ge=0, le=240, validation_alias="MOYAI_WORKER_SHUTDOWN_SECONDS",
    )
    http_shutdown_seconds: int = Field(
        default=20, ge=1, le=270, validation_alias="MOYAI_HTTP_SHUTDOWN_SECONDS",
    )
    modal_app: str = Field(
        default="moyai-pydantic",
        validation_alias=AliasChoices("MOYAI_MODAL_APP", "MODAL_APP_NAME"),
    )
    computer_lifetime: int = Field(default=3600, ge=60, le=86400, validation_alias="MOYAI_COMPUTER_TIMEOUT")
    computer_idle_timeout: int = Field(
        default=600, ge=60, le=86400, validation_alias="MOYAI_COMPUTER_IDLE_TIMEOUT"
    )
    model_request_limit: int = Field(default=100, ge=1, le=1000, validation_alias="MOYAI_MODEL_CALL_LIMIT")
    model_timeout_seconds: int = Field(default=300, ge=10, le=900, validation_alias="MOYAI_MODEL_TIMEOUT")
    secure_cookies: bool = Field(default=False, validation_alias="MOYAI_SECURE_COOKIES")
    public_url: str = Field(default="", validation_alias=AliasChoices("MOYAI_PUBLIC_URL", "PUBLIC_URL"))
    slack_signing_secret: SecretStr = Field(default=SecretStr(""), validation_alias="SLACK_SIGNING_SECRET")
    slack_bot_token: SecretStr = Field(default=SecretStr(""), validation_alias="SLACK_BOT_TOKEN")
    slack_team_id: str = Field(default="", validation_alias="SLACK_TEAM_ID")
    artifact_bucket: str = Field(default="", validation_alias="MOYAI_ARTIFACT_BUCKET")
    artifact_endpoint: str = Field(default="", validation_alias="MOYAI_ARTIFACT_ENDPOINT")
    artifact_region: str = Field(default="us-east-1", validation_alias="MOYAI_ARTIFACT_REGION")
    artifact_prefix: str = Field(default="moyai", validation_alias="MOYAI_NATIVE_ARTIFACT_PREFIX")
    artifact_access_key_id: SecretStr = Field(
        default=SecretStr(""), validation_alias="OBJECT_STORAGE_ACCESS_KEY_ID",
    )
    artifact_secret_access_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="OBJECT_STORAGE_SECRET_ACCESS_KEY",
    )
    artifact_session_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="OBJECT_STORAGE_SESSION_TOKEN",
    )

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys([
            self.default_model,
            *(value.strip() for value in self.model_catalog.split(",") if value.strip()),
        ]))

    @property
    def sqlalchemy_url(self) -> str:
        value = self.database_url
        if value.startswith(("postgres://", "postgresql://")):
            return "postgresql+asyncpg://" + value.split("://", 1)[1]
        return value or f"sqlite+aiosqlite:///{self.data_dir.resolve() / 'moyai.db'}"

    @property
    def postgres_url(self) -> str | None:
        if self.sqlalchemy_url.startswith("postgresql+asyncpg://"):
            return self.sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return None

    @cached_property
    def master_key(self) -> bytes:
        """Use an explicit key in production, or one private local development key."""
        supplied = self.encryption_key.get_secret_value()
        if supplied:
            if len(supplied) < 32:
                raise ValueError("ENCRYPTION_KEY must contain at least 32 characters.")
            return base64.urlsafe_b64encode(hashlib.sha256(supplied.encode()).digest())
        if self.postgres_url:
            raise ValueError("PostgreSQL deployments require a shared ENCRYPTION_KEY.")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.data_dir / ".master-key"
        if path.is_file():
            value = path.read_bytes()
        else:
            value = base64.urlsafe_b64encode(secrets.token_bytes(32))
            descriptor, temporary = tempfile.mkstemp(prefix=".key-", dir=self.data_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    value = path.read_bytes()
            finally:
                Path(temporary).unlink(missing_ok=True)
        if len(base64.urlsafe_b64decode(value)) != 32:
            raise ValueError("Invalid local master key.")
        return value

    def validate_worker(self, *, brokered: bool = False) -> None:
        if not self.gateway_key.get_secret_value() and not (
            self.product_mode == "existing" and brokered
        ):
            raise ValueError("Set LITELLM_API_KEY before starting an execution worker.")
        origin = urlsplit(self.gateway_url)
        if origin.username or origin.password or not (
            origin.scheme == "https"
            or (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("The model gateway must use HTTPS or an explicit loopback test endpoint.")

