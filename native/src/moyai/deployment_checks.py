"""Bounded connection checks for Docker installations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import httpx
import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import InvalidToken
from grpclib import GRPCError
from modal import Client as ModalClient
from modal.exception import Error as ModalError
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.service import RPCError

from .deployment_config import InstallationError
from .product_config import ProductConfigurationError, load_product_config
from .security import Security
from .settings import Settings
from .storage import ObjectStore


def settings(values: Mapping[str, str]) -> Settings:
    return cast(Settings, cast(type[BaseSettings], Settings)(
        _env_file=None,
        product_mode=values["MOYAI_PRODUCT_MODE"],
        data_dir=values["DATA_DIR"],
        database_url=values["MOYAI_DATABASE_URL"],
        database_schema=values["MOYAI_NATIVE_DATABASE_SCHEMA"],
        native_schema_mode="verify",
        encryption_key=SecretStr(
            values["MOYAI_NATIVE_ENCRYPTION_KEY"] if values["MOYAI_PRODUCT_MODE"] == "existing"
            else values["ENCRYPTION_KEY"]
        ),
        workspace_password=SecretStr(values["WORKSPACE_PASSWORD"]),
        gateway_url=values["LITELLM_API_BASE"],
        gateway_key=SecretStr(values["LITELLM_API_KEY"]),
        default_model=values["AGENT_MODEL"],
        temporal_address=values["TEMPORAL_ADDRESS"],
        temporal_namespace=values["TEMPORAL_NAMESPACE"],
        temporal_queue=values["MOYAI_NATIVE_TASK_QUEUE"],
        temporal_tls=values.get("TEMPORAL_TLS", "true").lower() == "true",
        temporal_api_key=SecretStr(values.get("TEMPORAL_API_KEY", "")),
        artifact_bucket=values["OBJECT_STORAGE_BUCKET"],
        artifact_endpoint=values.get("OBJECT_STORAGE_ENDPOINT", ""),
        artifact_region=values.get("OBJECT_STORAGE_REGION", "us-east-1"),
        artifact_prefix=values["MOYAI_NATIVE_OBJECT_PREFIX"],
        artifact_access_key_id=SecretStr(values.get("OBJECT_STORAGE_ACCESS_KEY_ID", "")),
        artifact_secret_access_key=SecretStr(values.get("OBJECT_STORAGE_SECRET_ACCESS_KEY", "")),
        artifact_session_token=SecretStr(values.get("OBJECT_STORAGE_SESSION_TOKEN", "")),
    ))


async def temporal(values: Mapping[str, str]) -> Client:
    client = await Client.connect(
        values["TEMPORAL_ADDRESS"], namespace=values["TEMPORAL_NAMESPACE"],
        tls=values.get("TEMPORAL_TLS", "true").lower() == "true",
        api_key=values.get("TEMPORAL_API_KEY") or None,
    )
    await client.workflow_service.describe_namespace(DescribeNamespaceRequest(
        namespace=values["TEMPORAL_NAMESPACE"],
    ))
    return client


async def check(values: Mapping[str, str], *, providers: bool = True) -> dict[str, str]:
    report = {}
    try:
        load_product_config(values)
    except ProductConfigurationError as error:
        raise InstallationError(str(error)) from None
    configured = settings(values)
    configured.validate_worker()
    component = "postgres"
    try:
        async with asyncio.timeout(90):
            async with await psycopg.AsyncConnection.connect(
                values["MOYAI_DATABASE_URL"], connect_timeout=5,
            ) as connection:
                await connection.execute("SELECT 1")
            report[component] = "connected"
            component = "object_storage"
            objects = ObjectStore(configured, Security(configured.master_key))
            marker = b"Moyai Docker installation storage check v1"
            reference = await objects.put(marker)
            if await objects.get(reference) != marker:
                raise InstallationError("The storage round trip returned different bytes.")
            report[component] = "verified"
            component = "temporal"
            await temporal(values)
            report[component] = "namespace_verified"
            if providers:
                component = "gateway"
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(
                        configured.gateway_url.rstrip("/") + "/models",
                        headers={"Authorization": "Bearer " + values["LITELLM_API_KEY"]},
                    )
                    response.raise_for_status()
                    catalog = response.json()
                    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
                        raise InstallationError("The gateway did not return an OpenAI-compatible model catalog.")
                    if not any(
                        isinstance(item, dict) and item.get("id") == values["AGENT_MODEL"] for item in catalog["data"]
                    ):
                        raise InstallationError("The selected model is not available to the supplied key.")
                report[component] = "model_listed"
                component = "modal"
                modal_client = await ModalClient.from_credentials.aio(values["MODAL_TOKEN_ID"], values["MODAL_TOKEN_SECRET"])
                await modal_client.hello.aio()
                report[component] = "authenticated"
        return report
    except InstallationError:
        raise
    except (
        OSError, ValueError, RuntimeError, KeyError, psycopg.Error, BotoCoreError, ClientError,
        InvalidToken, httpx.HTTPError, RPCError, GRPCError, ModalError,
    ):
        raise InstallationError(
            f"The {component} check failed; verify its endpoint, credentials, permissions, and network access."
        ) from None
