"""Dedicated authenticated receipt listener with its own bounded database pool."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.responses import JSONResponse

from .billing_storage_models import BillingStorageState
from .charge_receipts import ChargeReceiptBatch, ChargeReceiptIngestor, ReceiptResult
from .postgres import create_engine
from .product_deployment import ProductDeploymentError
from .product_schema import verify_product_schema

MAX_RECEIPT_BYTES = 256 * 1024


class ReceiptServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", env_file=None, hide_input_in_errors=True, populate_by_name=True,
    )

    database_url: SecretStr = Field(validation_alias="MOYAI_DATABASE_URL")
    native_schema: str = Field(
        validation_alias="MOYAI_NATIVE_SCHEMA", pattern=r"^[a-z][a-z0-9_]{0,62}$",
    )
    secret: SecretStr = Field(validation_alias="LITELLM_CHARGE_RECEIPT_SECRET", min_length=32)
    enabled: bool = Field(default=False, validation_alias="LITELLM_CHARGE_RECEIPTS_ENABLED")
    pool_size: int = Field(default=2, ge=1, le=8, validation_alias="LITELLM_CHARGE_RECEIPT_POOL_SIZE")
    local_rehearsal: bool = Field(default=False, validation_alias="MOYAI_PRODUCT_LOCAL_REHEARSAL")

    @model_validator(mode="after")
    def validate_receipt_configuration(self):
        value = self.database_url.get_secret_value()
        parsed = urlsplit(value)
        if (
            not self.enabled or parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
            or not parsed.path.strip("/") or parsed.fragment or any(character.isspace() for character in value)
            or self.native_schema in {"public", "information_schema"} or self.native_schema.startswith("pg_")
        ):
            raise ValueError("Enable receipts with a PostgreSQL URL and a dedicated native schema.")
        _ = parsed.port
        secret = self.secret.get_secret_value()
        if not secret.isascii() or any(character.isspace() for character in secret):
            raise ValueError("Use a non-whitespace ASCII receipt secret.")
        return self

    def check_listener(self, host: str) -> None:
        if not self.local_rehearsal:
            return
        try:
            allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise ProductDeploymentError("Local receipt rehearsal must bind a loopback listener.")


def create_receipt_application(settings: ReceiptServerSettings) -> FastAPI:
    schema = settings.native_schema
    secret = settings.secret.get_secret_value()
    engine = create_engine(
        settings.database_url.get_secret_value().replace("postgresql://", "postgresql+asyncpg://", 1).replace(
            "postgres://", "postgresql+asyncpg://", 1,
        ),
        pool_size=settings.pool_size, max_overflow=0, pool_timeout=3,
        connect_args={"server_settings": {
            "search_path": f'"{schema}",pg_catalog',
            "statement_timeout": "5000", "lock_timeout": "1000",
        }},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ingestor = ChargeReceiptIngestor(sessions)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await verify_product_schema(engine, schema=schema)
            async with sessions() as db:
                state = await db.get(BillingStorageState, 1)
                if (
                    state is None or state.phase != "consolidated" or state.native_schema != schema
                    or state.source_schema == schema
                ):
                    raise ProductDeploymentError("Consolidate the existing billing storage before enabling receipts.")
            yield
        finally:
            await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            async with sessions() as db:
                await db.execute(select(1))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/billing/receipts")
    async def accept(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + secret).encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.headers.get("content-encoding", "identity") != "identity":
            return JSONResponse({"error": "unsupported_encoding"}, status_code=415)
        try:
            async with asyncio.timeout(10):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_RECEIPT_BYTES:
                        return JSONResponse({"error": "receipt_batch_too_large"}, status_code=413)
                    body.extend(chunk)
                batch = ChargeReceiptBatch.model_validate_json(body)
                results: list[ReceiptResult] = []
                for receipt in batch.events:
                    results.append(await ingestor.accept(receipt))
        except ValidationError:
            return JSONResponse({"error": "invalid_receipt_batch"}, status_code=422)
        except (SQLAlchemyError, TimeoutError):
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        return JSONResponse({
            "acknowledged": [result.event_id for result in results if result.acknowledged],
            "rejected": [{"event_id": result.event_id, "error": result.error}
                         for result in results if not result.acknowledged],
        })

    return app
