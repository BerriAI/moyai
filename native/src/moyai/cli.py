"""Local and distributed entrypoints for the fresh Moyai application."""

from __future__ import annotations

import argparse
import asyncio
import getpass
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment

from .api import create_app
from .models import User
from .product_config import ProductConfigurationError, load_product_config
from .product_deployment import ProductDeploymentError, require_startable
from .product_start import _check_listener
from .runtime import Runtime
from .security import password_hash
from .settings import Settings
from .storage import encrypted_converter
from .worker import make_worker


async def run(args: argparse.Namespace) -> None:
    # Modal's client reads process configuration; Pydantic settings alone does not
    # export values from .env. Existing deployment environment variables win.
    load_dotenv(override=False)
    settings = Settings()
    if args.data_dir:
        settings.data_dir = Path(args.data_dir)
    product_config = None
    if settings.product_mode == "existing":
        product_config = load_product_config()
        if product_config is None or product_config.product_mode != "existing":
            raise ProductConfigurationError("Existing mode requires MOYAI_CORE=pydantic.")
        if args.command == "user":
            raise ProductDeploymentError("Existing product identities must be managed through product administration.")
        require_startable(product_config)
        if args.command in {"serve", "worker"}:
            raise ProductDeploymentError("Use python -m moyai.product_start serve for the selected product role.")
        if (
            args.command != "dev" or not product_config.local_rehearsal
            or product_config.role != "standalone"
        ):
            raise ProductDeploymentError("Existing-mode dev requires standalone local product rehearsal.")
        _check_listener(product_config, args.host)
    runtime = Runtime(settings, product_config=product_config)
    # Runtime applies the explicit core key, queue and separate native data path.
    settings = runtime.settings
    await runtime.start()
    try:
        if args.command == "user":
            password = getpass.getpass("New user's password: ")
            encoded = await asyncio.to_thread(password_hash, password)
            async with runtime.database.sessions.begin() as db:
                if await db.scalar(select(User.id).where(User.username == args.username)):
                    raise ValueError("That username already exists.")
                db.add(User(
                    username=args.username, display_name=args.name or args.username,
                    password=encoded, role="admin" if args.admin else "member",
                    slack_user_id=args.slack_user,
                ))
            print("User created.")
            return
        if args.command == "worker":
            settings.validate_worker()
            await runtime.connect()
            async with make_worker(runtime):
                await asyncio.Event().wait()
            return
        if args.command == "dev":
            settings.validate_worker()
            settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            environment = await WorkflowEnvironment.start_local(
                ip="127.0.0.1",
                namespace=settings.temporal_namespace,
                dev_server_database_filename=str(settings.data_dir.resolve() / "temporal.db"),
                plugins=[PydanticAIPlugin()],
                data_converter=encrypted_converter(runtime.security, runtime.objects),
            )
            async with environment:
                runtime.client = environment.client
                async with make_worker(runtime):
                    await uvicorn.Server(uvicorn.Config(
                        create_app(runtime), host=args.host, port=args.port,
                        log_level="info", access_log=False,
                    )).serve()
            return
        await uvicorn.Server(uvicorn.Config(
            create_app(runtime), host=args.host, port=args.port, log_level="info"
        )).serve()
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Moyai on Pydantic AI Harness")
    parser.add_argument("--data-dir", help="Private application data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "dev"):
        server = commands.add_parser(name)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", default=8796, type=int)
    commands.add_parser("worker")
    user = commands.add_parser("user")
    user.add_argument("username")
    user.add_argument("--name")
    user.add_argument("--admin", action="store_true")
    user.add_argument("--slack-user", help="Verified Slack member ID for this user")
    try:
        asyncio.run(run(parser.parse_args()))
    except (ProductConfigurationError, ProductDeploymentError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
