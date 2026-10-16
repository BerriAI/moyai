"""Bounded native maintenance transport; no runtime/agent/worker construction."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .product_config import ProductConfigurationError, load_product_config
from .product_deployment import ProductDeploymentError, require_startable
from .product_drain import ProductDrainError, ProductDrainService
from .product_role_authority import ProductOwnershipLost, ProductRoleAuthority


class ExpectedWorker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pid: int = Field(gt=0)
    backend_start: str
    source_policy: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_policy: str = Field(pattern=r"^[0-9a-f]{64}$")


class DrainCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1] = 1
    core: Literal["pydantic-v1"]
    native_schema: str
    task_queue: str
    expected_worker: ExpectedWorker | None = None
    expected_generation: int | None = Field(default=None, ge=0)
    request_key: str | None = Field(default=None, min_length=1, max_length=200)


async def execute(command: str, body: DrainCommand) -> dict:
    from app.runtime_compatibility import worker_fingerprint

    config = load_product_config()
    if config is None or config.product_mode != "existing":
        raise ProductDrainError("Native maintenance requires existing-product configuration.")
    require_startable(config)  # The production qualification gate remains closed.
    if body.native_schema != config.native_schema or body.task_queue != config.native_task_queue:
        raise ProductDrainError("The native maintenance scope changed.")
    if command != "status" and (
        config.role != "worker" or body.expected_worker is None
        or body.expected_generation is None or body.request_key is None
    ):
        raise ProductDrainError("A maintenance write requires its worker, generation and intent.")
    source = config.legacy_settings()
    expected = body.expected_worker
    authority = None
    if expected is not None:
        if expected.worker_policy != worker_fingerprint(source):
            raise ProductDrainError("The installed worker contract changed.")
        started = datetime.fromisoformat(expected.backend_start)
        if started.tzinfo is None:
            raise ProductDrainError("The expected worker identity is incomplete.")
        authority = ProductRoleAuthority(
            schema=config.product_schema or "", role="worker", owner_pid=expected.pid,
            owner_started=started, cluster_policy=expected.source_policy,
            role_policy=expected.worker_policy,
        )
    url = "postgresql+asyncpg://" + config.database_url.split("://", 1)[1]
    engine = create_async_engine(url, connect_args={"server_settings": {
        "search_path": f'"{config.native_schema}",pg_catalog',
        "statement_timeout": "5000", "lock_timeout": "5000",
    }}, pool_size=1, max_overflow=0)
    # Status without a live worker is also available at the zero-owner bootstrap.
    # Writes bind to the existing expected canonical owner; this command does not
    # join the cluster as another process or publish another role contract.
    sessions = authority.sessions(engine) if authority is not None else async_sessionmaker(
        engine, expire_on_commit=False,
    )
    service = ProductDrainService(
        sessions=lambda: sessions, authority=lambda: authority,
        native_schema=body.native_schema, task_queue=body.task_queue,
    )
    try:
        async with asyncio.timeout(15):
            if command == "status":
                return await service.status()
            assert body.expected_generation is not None and body.request_key is not None
            if command == "resume" and source.maintenance_drain:
                raise ProductDrainError("The installed worker is still configured to drain.")
            return await service.request(
                paused=command == "request", expected_generation=body.expected_generation,
                request_key=body.request_key,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["request", "status", "resume"])
    arguments = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ProductDrainError("The maintenance command exceeds its bound.")
        body = DrainCommand.model_validate_json(raw)
        result = asyncio.run(execute(arguments.command, body))
    except (ValidationError, ValueError, TypeError):
        parser.exit(1, "Invalid native maintenance command.\n")
    except (ProductDrainError, ProductOwnershipLost, ProductConfigurationError, ProductDeploymentError) as error:
        parser.exit(1, str(error) + "\n")
    except Exception:  # noqa: BLE001 - SQL/transport failures must not expose credentials
        # SQL and transport exceptions can contain credential-bearing inputs.
        parser.exit(1, "Native maintenance operation did not settle; inspect status before recovery.\n")
    print(json.dumps(result, sort_keys=True))
