"""Install, check, and run the versioned Docker package."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
from contextlib import nullcontext
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
    standalone_lock,
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
    if values["MOYAI_PRODUCT_MODE"] == "existing":
        ingress(environment, ready=False)
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
    if values["MOYAI_PRODUCT_MODE"] == "standalone":
        state["activation_pending"] = False
    write_state(environment, state)
    return {"installation": "initialized", "revision": state["revision"], "model_execution": "not_checked"}


def control_intent(values: dict[str, str], status: dict, request_key: str) -> dict:
    from .product_drain_cli import DrainCommand, ExpectedWorker

    if len(status["workers"]) != 1:
        raise InstallationError("Maintenance requires exactly one live, registered worker.")
    worker = {key: value for key, value in status["workers"][0].items() if key != "maintenance_drain"}
    return DrainCommand(
        core="pydantic-v1", native_schema=values["MOYAI_NATIVE_SCHEMA"],
        task_queue=values["MOYAI_NATIVE_TASK_QUEUE"], expected_generation=status["generation"],
        expected_worker=ExpectedWorker.model_validate(worker), request_key=request_key,
    ).model_dump(exclude_none=True)


async def control(
    values: dict[str, str], command: str, *, request_key: str | None = None, intent: dict | None = None,
) -> dict:
    from .product_drain_cli import DrainCommand, execute

    if values["MOYAI_PRODUCT_MODE"] != "existing":
        raise InstallationError("Coordinated maintenance commands require the split deployment.")
    os.environ.update(values)
    body = DrainCommand(
        core="pydantic-v1", native_schema=values["MOYAI_NATIVE_SCHEMA"],
        task_queue=values["MOYAI_NATIVE_TASK_QUEUE"],
    )
    if intent is not None:
        recorded = DrainCommand.model_validate(intent)
        if (recorded.native_schema, recorded.task_queue) != (body.native_schema, body.task_queue):
            raise InstallationError("The recorded maintenance scope changed.")
        return await execute(command, recorded)
    status = await execute("status", body)
    if command == "status":
        return status
    return await execute(command, DrainCommand.model_validate(control_intent(values, status, request_key or uuid4().hex)))


def confirms(status: dict, intent: dict, *, paused: bool) -> bool:
    worker = intent["expected_worker"]
    return (
        all(status.get(name) == intent[name] for name in ("protocol", "core", "native_schema", "task_queue", "request_key"))
        and status.get("generation") == intent["expected_generation"] + 1
        and status.get("paused") is paused
        and status.get("requester") == {"pid": worker["pid"], "backend_start": worker["backend_start"]}
        and status.get("source_policy") == worker["source_policy"]
    )


async def reconcile_control(values: dict[str, str], command: str, intent: dict) -> dict:
    """Inspect a lost acknowledgement before repeating the original fenced command."""
    status = await control(values, "status")
    paused = command == "request"
    if confirms(status, intent, paused=paused):
        return status
    workers = [{key: value for key, value in worker.items() if key != "maintenance_drain"}
               for worker in status["workers"]]
    if (
        status["generation"] != intent["expected_generation"] or status["paused"] is paused
        or workers != [intent["expected_worker"]]
        or not paused and status["state"] != "drained"
    ):
        raise InstallationError("The maintenance generation or worker changed; inspect the recorded operation.")
    receipt = await control(values, command, intent=intent)
    if not confirms(receipt, intent, paused=paused):
        raise InstallationError("The worker did not confirm the recorded maintenance operation; ingress remains closed.")
    return receipt


async def health(values: dict[str, str]) -> None:
    identities = {}
    ports = {"api": 8787, "coordinator": 8788, "broker": 8789, "worker": 8790}
    async with httpx.AsyncClient(timeout=10) as client:
        for role in ("coordinator", "broker", "worker", "api"):
            response = await client.get(
                (f"http://127.0.0.1:{ports[role]}/health"
                 if values.get("MOYAI_COLOCATED_ROLES") == "true" else f"http://{role}:8787/health"), headers={"Host": httpx.URL(values["MOYAI_PUBLIC_URL"]).netloc.decode()},
            )
            response.raise_for_status()
            if (
                response.headers.get("X-Moyai-Core") != "pydantic-v1"
                or response.headers.get("X-Moyai-Role") != role
                or response.headers.get("X-Moyai-Build") != values["MOYAI_BUILD_SHA"]
                or not response.headers.get("X-Moyai-Owner")
            ):
                raise InstallationError("A running role has a different release or runtime identity.")
            identities[role] = response.headers
        headers = {"Cache-Control": "no-cache"}
        if values.get("CLOUDFLARE_ACCESS_CLIENT_ID"):
            headers.update({
                "CF-Access-Client-Id": values["CLOUDFLARE_ACCESS_CLIENT_ID"],
                "CF-Access-Client-Secret": values.get("CLOUDFLARE_ACCESS_CLIENT_SECRET", ""),
            })
        try:
            from app.public_url import broker_base_url
            endpoint = broker_base_url(values["MOYAI_PUBLIC_URL"], values.get("MOYAI_BROKER_URL", ""))
            response = await client.get(endpoint + "/broker/health", headers=headers)
            response.raise_for_status()
        except httpx.HTTPError:
            raise InstallationError("The public broker route is unavailable; execution was not resumed.") from None
        if any(response.headers.get(name) != identities["broker"].get(name) for name in (
            "X-Moyai-Core", "X-Moyai-Role", "X-Moyai-Build", "X-Moyai-Owner",
        )):
            raise InstallationError("The public broker route does not reach the verified broker owner.")


def ingress(environment: dict[str, str], *, ready: bool) -> None:
    """Publish only a nonsecret marker on the proxy's dedicated volume."""
    directory = state_path(environment).parent / "ingress"
    directory.mkdir(exist_ok=True, mode=0o755)
    os.chmod(directory, 0o755)
    marker = directory / "ready"
    if ready:
        marker.touch(mode=0o644)
    else:
        marker.unlink(missing_ok=True)


async def activate(environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    ingress(environment, ready=False)
    if state.get("upgrade"):
        raise InstallationError("Complete the pending maintenance upgrade before opening ingress.")
    await health(values)
    status = await control(values, "status")
    if state.get("activation_pending"):
        if "activation_command" not in state:
            if status["generation"] != 0 or status["request_key"] != "bootstrap" or status["state"] != "drained":
                raise InstallationError("Initial activation requires the untouched, drained bootstrap receipt.")
            if any(status["counts"].values()):
                raise InstallationError("Initial activation cannot adopt existing queued or unfinished work.")
            key = state.get("activation_intent") or uuid4().hex
            state.update(activation_intent=key, activation_command=control_intent(values, status, key))
            write_state(environment, state)
        if not confirms(status, state["activation_command"], paused=False) and any(status["counts"].values()):
            raise InstallationError("Initial activation cannot adopt existing queued or unfinished work.")
        status = await reconcile_control(values, "resume", state["activation_command"])
        state["activation_pending"] = False
        write_state(environment, state)
    if status["paused"]:
        raise InstallationError("The installation is in maintenance; inspect and resume that operation explicitly.")
    ingress(environment, ready=True)
    return {"installation": "active", "revision": state["revision"], "model_execution": "not_checked"}


async def begin_upgrade(args: argparse.Namespace, environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or args.revision == state["revision"]:
        raise InstallationError("Select a different release image with its full embedded revision.")
    upgrade = state.get("upgrade")
    if state["revision"] != values["MOYAI_BUILD_SHA"] or (
        upgrade is None and state["phase"] != "initialized"
    ) or (
        upgrade is not None and (
            state["phase"] not in {"initialized", "upgrading"} or upgrade["source"] != state["revision"]
            or upgrade["target"] != args.revision or upgrade["phase"] not in {"requesting_drain", "drained"}
        )
    ):
        raise InstallationError("The source installation is not a settled baseline.")
    await health(values)
    status = await control(values, "status")
    if upgrade is None:
        if status["paused"]:
            raise InstallationError("Finish the existing maintenance operation before starting an upgrade.")
        intent = uuid4().hex
        upgrade = state["upgrade"] = {
            "source": state["revision"], "target": args.revision, "intent": intent, "phase": "requesting_drain",
            "drain_command": control_intent(values, status, intent),
        }
        write_state(environment, state)
    ingress(environment, ready=False)
    if "drain_command" not in upgrade:
        raise InstallationError("The earlier upgrade lacks its exact drain command; inspect its native receipt before recovery.")
    await reconcile_control(values, "request", upgrade["drain_command"])
    async with asyncio.timeout(args.timeout):
        while True:
            status = await control(values, "status")
            if not confirms(status, upgrade["drain_command"], paused=True):
                raise InstallationError("The drain generation changed while waiting; inspect the recorded operation.")
            if status["state"] == "drained":
                break
            await asyncio.sleep(2)
    state["phase"] = "upgrading"
    upgrade.update(phase="drained", generation=status["generation"])
    write_state(environment, state)
    return {"upgrade": "drained", "source": state["revision"], "target": args.revision}


async def apply_upgrade(environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    upgrade = state.get("upgrade", {})
    if (
        state["phase"] != "upgrading" or upgrade.get("phase") != "drained"
        or upgrade.get("target") != values["MOYAI_BUILD_SHA"]
    ):
        raise InstallationError("Use the recorded target image after the source installation has drained.")
    ingress(environment, ready=False)
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


async def standalone_unsettled(values: dict[str, str]) -> dict[str, int]:
    from sqlalchemy import func, select

    from .database import Database
    from .models import Turn
    from .product_drain import TERMINAL_TURNS, ProductDrainService
    from .security import Security

    configured = deployment_checks.settings(values)
    database = Database(configured, Security(configured.master_key))
    try:
        async with database.sessions.begin() as session:
            turns = await session.scalar(select(func.count()).select_from(Turn).where(
                Turn.status.not_in(TERMINAL_TURNS),
            ))
            effects, requests = await ProductDrainService._unsettled(session)
            return {"turns": turns or 0, "effects": effects, "requests": requests}
    finally:
        await database.close()


async def standalone_upgrade(
    args: argparse.Namespace, environment: dict[str, str], state: dict, values: dict[str, str],
) -> dict:
    if (
        values["MOYAI_PRODUCT_MODE"] != "standalone" or state["phase"] != "initialized"
        or state.get("upgrade") or state["revision"] != args.source_revision
        or state["revision"] == values["MOYAI_BUILD_SHA"] or state.get("standalone_lock_protocol") != 1
    ):
        raise InstallationError("Standalone upgrades require the original stopped installation and a new image revision.")
    with standalone_lock(environment):
        async with deployment_bootstrap.installation_lock(values):
            counts = await standalone_unsettled(values)
            if any(counts.values()):
                raise InstallationError(
                    "Unfinished standalone work remains. Restart the old image and finish or reconcile it before upgrading."
                )
            state.update(phase="upgrading", upgrade={
                "source": state["revision"], "target": values["MOYAI_BUILD_SHA"], "phase": "migrating",
            })
            write_state(environment, state)
            schemas = await deployment_bootstrap.provision(values, fresh=False)
            state.update(phase="initialized", revision=values["MOYAI_BUILD_SHA"], schemas=schemas)
            state["last_upgrade"] = state.pop("upgrade")
            state["last_upgrade"]["phase"] = "completed"
            write_state(environment, state)
    return {"upgrade": "prepared", "revision": state["revision"]}


def validate_configuration(environment: dict[str, str]) -> dict:
    state = read_state(environment)
    values = configuration(environment, state)
    if state is not None:
        if state["phase"] != "initialized" or state.get("upgrade"):
            raise InstallationError("An installation or maintenance operation is pending; inspect it before startup.")
        if state["revision"] != values["MOYAI_BUILD_SHA"]:
            raise InstallationError("The image revision changed; use the maintenance upgrade procedure.")
    else:
        values = configuration(environment, new_state(environment, values))
    deployment_checks.validate(values)
    return {"configuration": "valid", "service_connections": "not_checked"}


async def ready(environment: dict[str, str], state: dict, values: dict[str, str]) -> dict:
    if (
        state["phase"] != "initialized" or state["revision"] != values["MOYAI_BUILD_SHA"]
        or state.get("upgrade") or state.get("activation_pending")
    ):
        raise InstallationError("Startup or maintenance is incomplete; inspect setup and activation.")
    if values["MOYAI_PRODUCT_MODE"] == "existing":
        await health(values)
        status = await control(values, "status")
        if status["paused"] or not (state_path(environment).parent / "ingress/ready").is_file():
            raise InstallationError("The installation has not opened ingress; inspect activation or maintenance.")
    else:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get("http://app:8787/health")
            response.raise_for_status()
            body = response.json()
            expected = {"status": "ok", "engine": "pydantic-ai", "durability": "temporal"}
            if not isinstance(body, dict) or any(body.get(key) != value for key, value in expected.items()):
                raise InstallationError("The application did not return its expected health response.")
    return {
        "installation": "serving", "http": "healthy", "revision": state["revision"],
        "url": values["MOYAI_PUBLIC_URL"], "model_execution": "not_checked",
    }


async def dispatch(args: argparse.Namespace, environment: dict[str, str]) -> dict:
    if args.command == "validate":
        return validate_configuration(environment)
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
    if args.command == "ready":
        return await ready(environment, state, values)
    if args.command == "begin-upgrade":
        return await begin_upgrade(args, environment, state, values)
    if args.command == "apply-upgrade":
        return await apply_upgrade(environment, state, values)
    if args.command == "standalone-upgrade":
        return await standalone_upgrade(args, environment, state, values)
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
        ingress(environment, ready=False)
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
        ingress(environment, ready=False)
        await health(values)
        upgrade = state.get("upgrade")
        if upgrade:
            if upgrade["phase"] not in {"awaiting_resume", "resuming"}:
                raise InstallationError("Inspect the pending upgrade and exact maintenance generation before resume.")
            if upgrade["phase"] == "awaiting_resume":
                status = await control(values, "status")
                if status["generation"] != upgrade["generation"] or status["state"] != "drained":
                    raise InstallationError("Inspect the pending upgrade and exact maintenance generation before resume.")
                upgrade.update(phase="resuming", resume_command=control_intent(values, status, uuid4().hex))
                write_state(environment, state)
            if "resume_command" not in upgrade:
                raise InstallationError("The earlier upgrade lacks its exact resume command; inspect its native receipt.")
            status = await reconcile_control(values, "resume", upgrade["resume_command"])
        else:
            status = await control(values, "status")
            if status["paused"]:
                status = await control(values, "resume")
        if status["paused"]:
            raise InstallationError("The worker did not confirm resume; ingress remains closed.")
        if upgrade:
            state["last_upgrade"] = state.pop("upgrade")
            state["last_upgrade"]["phase"] = "completed"
            write_state(environment, state)
        ingress(environment, ready=True)
        return status
    raise InstallationError("Unsupported installation command.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "initialize", "validate", "ready", "check", "activate", "status", "resume", "namespace", "apply-upgrade",
        "recover-setup",
    ):
        commands.add_parser(command)
    upgrade = commands.add_parser("begin-upgrade")
    upgrade.add_argument("--revision", required=True)
    upgrade.add_argument("--timeout", type=int, default=900)
    offline = commands.add_parser("standalone-upgrade")
    offline.add_argument("--source-revision", required=True)
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
            lock = standalone_lock(environment) if args.role == "standalone" else nullcontext()
            with lock:
                start(["serve", "--host", "0.0.0.0", "--port", "8787"])
            return
        if args.command in {"validate", "ready", "status"}:
            # Atomic state-file replacement gives readers a coherent snapshot.
            # Read-only checks must not wait behind a long-running maintenance command.
            print(json.dumps(asyncio.run(dispatch(args, environment)), sort_keys=True))
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
