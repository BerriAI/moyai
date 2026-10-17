"""Selected native entrypoint; never imports app.main or a legacy executor."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import signal
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, cast

from pydantic_settings import BaseSettings

from .product_config import ProductConfig, ProductConfigurationError, load_product_config
from .product_deployment import (
    ProductDeploymentError,
    check_runtime_dependencies,
    require_startable,
    verify_product_schema,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .runtime import Runtime
    from .settings import Settings


def _settings(config: ProductConfig) -> Settings:
    from .settings import Settings

    return cast(Settings, cast(type[BaseSettings], Settings)(_env_file=None, **config.native_overrides()))


def _runtime(config: ProductConfig) -> Runtime:
    from .runtime import Runtime
    return Runtime(_settings(config), product_config=config)


def _check_listener(config: ProductConfig, host: str) -> None:
    if not config.local_rehearsal:
        return
    try:
        allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise ProductDeploymentError("Local product rehearsal must bind a loopback listener.")


def create_application(config: ProductConfig, *, host: str = "127.0.0.1") -> FastAPI:
    """Return the real native API, with the selected lifecycle and role flags."""
    plan = require_startable(config)  # Must precede constructing any resources.
    _check_listener(config, host)
    if not plan.serve_api:
        raise ProductDeploymentError("This role does not own an API listener.")
    check_runtime_dependencies()
    from .api import create_app
    from .product_lifecycle import ProductRoleLifecycle, ProductRoleMiddleware
    from .worker import make_worker

    runtime = _runtime(config)
    lifecycle = (
        ProductRoleLifecycle(runtime)
        if config.product_mode == "existing" and config.role != "standalone" else None
    )
    app = create_app(runtime, run_dispatcher=plan.run_dispatcher and lifecycle is None)
    app.state.product_lifecycle = lifecycle
    if lifecycle is not None:
        app.add_middleware(ProductRoleMiddleware, lifecycle=lifecycle)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            if config.product_mode == "existing":
                await asyncio.to_thread(verify_product_schema, config)
            await runtime.start()
            async with AsyncExitStack() as stack:
                if lifecycle is not None:
                    await stack.enter_async_context(lifecycle.lifespan())
                elif plan.run_worker:
                    runtime.settings.validate_worker(
                        brokered=bool(runtime.product_inference and runtime.product_inference.remote),
                    )
                    await runtime.connect()
                    await stack.enter_async_context(make_worker(runtime))
                await stack.enter_async_context(original_lifespan(application))
                yield
        finally:
            await runtime.close()

    app.router.lifespan_context = lifespan
    return app


async def serve_worker(config: ProductConfig) -> None:
    plan = require_startable(config)
    if config.role != "worker" or not plan.run_worker:
        raise ProductDeploymentError("The worker entrypoint requires the worker role.")
    check_runtime_dependencies()
    from .worker import make_worker

    runtime = _runtime(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    try:
        for value in signals:
            loop.add_signal_handler(value, stop.set)
        await asyncio.to_thread(verify_product_schema, config)
        runtime.settings.validate_worker(
            brokered=bool(runtime.product_inference and runtime.product_inference.remote),
        )
        await runtime.start()
        await runtime.connect()
        async with make_worker(runtime):
            await stop.wait()
    finally:
        for value in signals:
            loop.remove_signal_handler(value)
        await runtime.close()


async def prepare_native_schema(config: ProductConfig, *, apply: bool) -> dict[str, str]:
    """Explicit migration/verification; never starts a Runtime or an agent."""
    if config.product_mode != "existing" or not config.native_schema or not config.product_schema:
        raise ProductDeploymentError("Native schema commands require explicit existing-product schemas.")
    check_runtime_dependencies()
    await asyncio.to_thread(verify_product_schema, config)
    import asyncpg
    from sqlalchemy.exc import SQLAlchemyError

    from .postgres import create_engine
    from .product_continuation_schema import (
        install_source_continuation_fence,
        verify_source_continuation_fence,
    )
    from .product_schema import migrate_product_schema
    from .product_schema import verify_product_schema as verify_native_schema
    from .stores import NativeStores

    settings = _settings(config)
    engine = create_engine(settings.sqlalchemy_url, pool_pre_ping=True)
    stores = NativeStores(settings)
    try:
        if apply:
            revision = await migrate_product_schema(
                engine, schema=config.native_schema, source_schema=config.product_schema,
            )
            async with engine.begin() as connection:
                await install_source_continuation_fence(
                    connection, source_schema=config.product_schema, native_schema=config.native_schema,
                )
            # Public SDK operations provision its schema and write the reviewed
            # shape/version receipt. Ordinary startup only verifies that receipt.
            await stores.start(provision=True)
        else:
            await verify_native_schema(engine, schema=config.native_schema)
            await stores.start()
            revision = "verified"
        await verify_source_continuation_fence(
            engine, source_schema=config.product_schema, native_schema=config.native_schema,
        )
        return {
            "core": config.core_identity, "native_schema": config.native_schema,
            "app_schema": revision, "harness_schema": "provisioned" if apply else "verified",
            # Migration receipts do not assert role health or authorize resume.
            "deployment_readiness": "not_checked",
        }
    except (SQLAlchemyError, asyncpg.PostgresError, ValueError, RuntimeError):
        raise ProductDeploymentError(
            "Native schema operation failed; check the reviewed app and Harness migration receipts."
        ) from None
    finally:
        try:
            await stores.close()
        finally:
            await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    receipts = commands.add_parser("receipts")
    receipts.add_argument("--host", default="127.0.0.1")
    receipts.add_argument("--port", type=int, default=8788)
    commands.add_parser("check")
    commands.add_parser("verify")
    provision = commands.add_parser("provision")
    provision.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "receipts":
            import uvicorn
            from pydantic import ValidationError

            from .charge_receipt_server import ReceiptServerSettings, create_receipt_application

            try:
                settings = cast(ReceiptServerSettings, cast(type[BaseSettings], ReceiptServerSettings)())
            except ValidationError:
                raise ProductDeploymentError("Configure the receipt listener's database, native schema, and secret.") from None
            settings.check_listener(args.host)
            uvicorn.run(
                create_receipt_application(settings), host=args.host, port=args.port,
                workers=1, proxy_headers=False, limit_concurrency=32,
                timeout_graceful_shutdown=15,
            )
            return
        config = load_product_config()
        if config is None:
            raise ProductConfigurationError("This entrypoint requires MOYAI_CORE=pydantic.")
        if args.command in {"provision", "verify"}:
            print(json.dumps(asyncio.run(prepare_native_schema(config, apply=args.command == "provision"))))
            return
        plan = require_startable(config)
        versions = check_runtime_dependencies()
        if args.command == "check":
            # Configuration/dependency check only; do not claim schema or service readiness.
            print(json.dumps({
                "core": config.core_identity, "mode": config.product_mode, "role": config.role,
                "configuration": "valid", "dependencies": versions, "readiness": "not_checked",
            }))
            return
        if plan.run_worker and not plan.serve_api:
            asyncio.run(serve_worker(config))
            return
        import uvicorn
        app = create_application(config, host=args.host)
        server = uvicorn.Server(uvicorn.Config(
            app, host=args.host, port=args.port, workers=1,
            proxy_headers=False, timeout_graceful_shutdown=_settings(config).http_shutdown_seconds,
        ))
        lifecycle = getattr(app.state, "product_lifecycle", None)
        if lifecycle is not None:
            lifecycle.retire = lambda: setattr(server, "should_exit", True)
        server.run()
        if lifecycle is not None and lifecycle.failed:
            # Cleanup has completed, but loss of the canonical owner is a
            # failed process lifetime, not a successful graceful shutdown.
            raise SystemExit(1)
    except (ProductConfigurationError, ProductDeploymentError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
