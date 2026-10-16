"""Install, check, and run the versioned Docker package."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
from uuid import uuid4

import asyncpg
import httpx
import psycopg
from sqlalchemy.exc import SQLAlchemyError
from temporalio.service import RPCError

from . import deployment_bootstrap, deployment_checks
from .deployment_config import (
    InstallationError,
    configuration,
    new_state,
    read_state,
    state_path,
    write_state,
)


async def initialize(environment: dict[str, str]) -> dict:
    state = read_state(environment)
    values = configuration(environment, state)
    async with deployment_bootstrap.installation_lock(values):
        return await initialize_locked(environment, state, values)


async def initialize_locked(environment: dict[str, str], state: dict | None, values: dict[str, str]) -> dict:
    if state is not None:
        if state["phase"] != "initialized":
            raise InstallationError("An earlier setup or upgrade is incomplete; inspect its state before recovery.")
        if state["revision"] != values["MOYAI_BUILD_SHA"]:
            raise InstallationError("The image revision changed; use the maintenance upgrade procedure.")
        await deployment_checks.check(values)
        await deployment_bootstrap.verify(values)
        return {"installation": "verified", "revision": state["revision"], "model_execution": "not_checked"}
    await asyncio.to_thread(deployment_bootstrap.require_empty, values)
    state = new_state(environment, values)
    values = configuration(environment, state)
    await deployment_checks.check(values)
    write_state(environment, state)
    try:
        schemas = await deployment_bootstrap.provision(values, fresh=True)
    except BaseException:
        state["phase"] = "failed"
        write_state(environment, state)
        raise
    state.update(phase="initialized", schemas=schemas)
    write_state(environment, state)
    return {"installation": "initialized", "revision": state["revision"], "model_execution": "not_checked"}


async def control(values: dict[str, str], command: str, *, request_key: str | None = None) -> dict:
    from .product_drain_cli import DrainCommand, ExpectedWorker, execute

    if values["MOYAI_PRODUCT_MODE"] != "existing":
        raise InstallationError("Coordinated maintenance commands require the split deployment.")
    os.environ.update(values)
    body = DrainCommand(
        core="pydantic-v1", native_schema=values["MOYAI_NATIVE_SCHEMA"],
        task_queue=values["MOYAI_NATIVE_TASK_QUEUE"],
    )
    status = await execute("status", body)
    if command == "status":
        return status
    if len(status["workers"]) != 1:
        raise InstallationError("Maintenance requires exactly one live, registered worker.")
    worker = {key: value for key, value in status["workers"][0].items() if key != "maintenance_drain"}
    intent = DrainCommand(
        **body.model_dump(exclude_none=True), expected_generation=status["generation"],
        expected_worker=ExpectedWorker.model_validate(worker), request_key=request_key or uuid4().hex,
    )
    return await execute(command, intent)


async def health(values: dict[str, str]) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        for role in ("coordinator", "broker", "worker", "api"):
            response = await client.get(
                f"http://{role}:8787/health", headers={"Host": httpx.URL(values["MOYAI_PUBLIC_URL"]).netloc.decode()},
            )
            response.raise_for_status()
            if (
                response.headers.get("X-Moyai-Core") != "pydantic-v1"
                or response.headers.get("X-Moyai-Role") != role
                or response.headers.get("X-Moyai-Build") != values["MOYAI_BUILD_SHA"]
            ):
                raise InstallationError("A running role has a different release or runtime identity.")


async def activate(environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    if state.get("upgrade"):
        raise InstallationError("Complete the pending maintenance upgrade before opening ingress.")
    await health(values)
    status = await control(values, "status")
    if state.get("activation_pending"):
        if status["generation"] != 0 or status["request_key"] != "bootstrap" or status["state"] != "drained":
            raise InstallationError("Initial activation requires the untouched, drained bootstrap receipt.")
        if any(status["counts"].values()):
            raise InstallationError("Initial activation cannot adopt existing queued or unfinished work.")
        key = state.get("activation_intent") or uuid4().hex
        state["activation_intent"] = key
        write_state(environment, state)
        status = await control(values, "resume", request_key=key)
        state["activation_pending"] = False
        write_state(environment, state)
    if status["paused"]:
        raise InstallationError("The installation is in maintenance; inspect and resume that operation explicitly.")
    return {"installation": "active", "revision": state["revision"], "model_execution": "not_checked"}


async def begin_upgrade(args: argparse.Namespace, environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or args.revision == state["revision"]:
        raise InstallationError("Select a different release image with its full embedded revision.")
    if state["phase"] != "initialized" or state.get("upgrade") or state["revision"] != values["MOYAI_BUILD_SHA"]:
        raise InstallationError("The source installation is not a settled baseline.")
    await health(values)
    status = await control(values, "status")
    if status["paused"]:
        raise InstallationError("Finish the existing maintenance operation before starting an upgrade.")
    intent = uuid4().hex
    state["upgrade"] = {
        "source": state["revision"], "target": args.revision, "intent": intent, "phase": "requesting_drain",
    }
    write_state(environment, state)
    await control(values, "request", request_key=intent)
    async with asyncio.timeout(args.timeout):
        while True:
            status = await control(values, "status")
            if status["state"] == "drained":
                break
            await asyncio.sleep(2)
    state["phase"] = "upgrading"
    state["upgrade"].update(phase="drained", generation=status["generation"])
    write_state(environment, state)
    return {"upgrade": "drained", "source": state["revision"], "target": args.revision}


async def apply_upgrade(environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    upgrade = state.get("upgrade", {})
    if (
        state["phase"] != "upgrading" or upgrade.get("phase") != "drained"
        or upgrade.get("target") != values["MOYAI_BUILD_SHA"]
    ):
        raise InstallationError("Use the recorded target image after the source installation has drained.")
    status = await control(values, "status")
    if status["state"] != "drained" or status["generation"] != upgrade["generation"] or status["workers"]:
        raise InstallationError("The source worker must be stopped at the recorded drained generation.")
    upgrade["phase"] = "migrating"
    write_state(environment, state)
    schemas = await deployment_bootstrap.provision(values, fresh=False)
    state.update(phase="initialized", revision=values["MOYAI_BUILD_SHA"], schemas=schemas)
    upgrade["phase"] = "awaiting_resume"
    write_state(environment, state)
    return {"upgrade": "prepared", "revision": state["revision"], "execution": "paused"}


async def dispatch(args: argparse.Namespace, environment: dict[str, str]) -> dict:
    if args.command == "namespace":
        if environment.get("MOYAI_TEMPORAL_MODE") != "bundled":
            raise InstallationError("Namespace creation is restricted to the bundled Temporal server.")
        from google.protobuf.duration_pb2 import Duration
        from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
        from temporalio.client import Client
        from temporalio.service import RPCError, RPCStatusCode

        values = configuration(environment, None)
        async with asyncio.timeout(60):
            client = await Client.connect(values["TEMPORAL_ADDRESS"], namespace=values["TEMPORAL_NAMESPACE"])
            request = DescribeNamespaceRequest(namespace=values["TEMPORAL_NAMESPACE"])
            try:
                await client.workflow_service.describe_namespace(request)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise InstallationError("Cannot verify the bundled Temporal namespace.") from None
                await client.workflow_service.register_namespace(RegisterNamespaceRequest(
                    namespace=values["TEMPORAL_NAMESPACE"],
                    workflow_execution_retention_period=Duration(seconds=7 * 86400),
                ))
                await client.workflow_service.describe_namespace(request)
        return {"temporal_namespace": "ready"}
    if args.command == "initialize":
        return await initialize(environment)
    state = read_state(environment)
    if state is None:
        raise InstallationError("Run the initialization service first.")
    values = configuration(environment, state, role="worker")
    if args.command == "begin-upgrade":
        return await begin_upgrade(args, environment, state, values)
    if args.command == "apply-upgrade":
        return await apply_upgrade(environment, state, values)
    if args.command == "recover-setup":
        if state["phase"] not in {"prepared", "failed"} or state["revision"] != values["MOYAI_BUILD_SHA"]:
            raise InstallationError("Setup recovery requires the original failed installation and image.")
        async with deployment_bootstrap.installation_lock(values):
            await deployment_checks.check(values)
            schemas = await deployment_bootstrap.provision(values, fresh=True)
        state.update(phase="initialized", schemas=schemas)
        write_state(environment, state)
        return {"installation": "initialized", "revision": state["revision"]}
    if args.command == "check":
        checks = await deployment_checks.check(values)
        schema = await deployment_bootstrap.verify(values)
        return {"checks": checks, "schemas": schema, "model_execution": "not_checked"}
    if args.command == "activate":
        if state["phase"] != "initialized" or state["revision"] != values["MOYAI_BUILD_SHA"]:
            raise InstallationError("Activation requires the initialized image revision.")
        return await activate(environment, state, values)
    if args.command == "status":
        return await control(values, "status")
    if args.command == "pause":
        await control(values, "request")
        async with asyncio.timeout(args.timeout):
            while True:
                status = await control(values, "status")
                if status["state"] == "drained":
                    return status
                await asyncio.sleep(2)
    if args.command == "resume":
        if state["phase"] != "initialized" or state["revision"] != values["MOYAI_BUILD_SHA"]:
            raise InstallationError("Resume requires the prepared image revision.")
        await health(values)
        upgrade = state.get("upgrade")
        if upgrade:
            status = await control(values, "status")
            if (
                upgrade["phase"] != "awaiting_resume" or status["generation"] != upgrade["generation"]
                or status["state"] != "drained"
            ):
                raise InstallationError("Inspect the pending upgrade and exact maintenance generation before resume.")
            upgrade["phase"] = "resuming"
            write_state(environment, state)
        status = await control(values, "resume")
        if upgrade:
            state["last_upgrade"] = state.pop("upgrade")
            state["last_upgrade"]["phase"] = "completed"
            write_state(environment, state)
        return status
    raise InstallationError("Unsupported installation command.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("initialize", "check", "activate", "status", "resume", "namespace", "apply-upgrade", "recover-setup"):
        commands.add_parser(command)
    upgrade = commands.add_parser("begin-upgrade")
    upgrade.add_argument("--revision", required=True)
    upgrade.add_argument("--timeout", type=int, default=900)
    pause = commands.add_parser("pause")
    pause.add_argument("--timeout", type=int, default=900)
    serve = commands.add_parser("serve")
    serve.add_argument("role", choices=["standalone", "api", "coordinator", "broker", "worker"])
    commands.add_parser("password")
    args = parser.parse_args()
    environment = dict(os.environ)
    try:
        if args.command in {"serve", "password"}:
            state = read_state(environment)
            if state is None or state["phase"] != "initialized":
                raise InstallationError("Wait for initialization to complete before starting the application.")
            if args.command == "password":
                print(state["secrets"]["WORKSPACE_PASSWORD"])
                return
            values = configuration(environment, state, role=args.role)
            if state["revision"] != values["MOYAI_BUILD_SHA"]:
                raise InstallationError("Use the initialized image revision or the maintenance upgrade procedure.")
            if (args.role == "standalone") != (values["MOYAI_PRODUCT_MODE"] == "standalone"):
                raise InstallationError("The selected role does not match the persisted installation topology.")
            os.environ.update(values)
            from .product_start import main as start
            start(["serve", "--host", "0.0.0.0", "--port", "8787"])
            return
        lock = state_path(environment).parent / ".installation.lock"
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock.open("a") as handle:
            os.chmod(lock, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            print(json.dumps(asyncio.run(dispatch(args, environment)), sort_keys=True))
    except InstallationError as error:
        parser.exit(1, str(error) + "\n")
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, psycopg.Error, asyncpg.PostgresError, SQLAlchemyError, RPCError):
        parser.exit(1, "The installation operation failed. Inspect component health and the persisted state before retrying.\n")


if __name__ == "__main__":
    main()
