"""Typed SDK tools; only the trusted tool activity materializes credential values."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

from app.credentials import ONEPASSWORD_GUIDANCE, CredentialRequest, ListCredentials, RunCredential
from fastapi import HTTPException
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import Toolset, durable_operation
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import update

from .contracts import RunScope
from .effects import confirm_effect, effect_id
from .models import Effect, now
from .product_credential_execution import ProductCredentialExecution

TOOLSET_ID = "moyai_product_credentials_v1"
READ = {"annotations": {"readOnlyHint": True}}
WRITE = {"annotations": {"readOnlyHint": False}}

# No credential files are written to a snapshot-able path. Linux anonymous
# memfds stay open only for this foreground command and disappear on process exit.
COMMAND_GUEST = r"""
import json, os, signal, subprocess, sys
payload = json.loads(os.environ.pop("MOYAI_NATIVE_CREDENTIAL_PAYLOAD"))
env = dict(os.environ)
fds = []
try:
    for binding in payload["bindings"]:
        if binding["format"] == "env":
            for key, value in json.loads(binding["value"]).items():
                if key in payload["names"]:
                    raise ValueError("Overlapping credential environment variables.")
                payload["names"].append(key)
                env[key] = value
        else:
            key = binding["env_var"]
            if key in payload["names"]:
                raise ValueError("Overlapping credential environment variables.")
            payload["names"].append(key)
            fd = os.memfd_create("moyai-credential", os.MFD_CLOEXEC)
            os.write(fd, binding["value"].encode())
            os.lseek(fd, 0, 0)
            fds.append(fd)
            env[key] = "/proc/self/fd/" + str(fd)
    process = subprocess.Popen(
        ["/bin/sh", "-c", payload["command"]], env=env, pass_fds=fds,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        code = process.wait(timeout=payload["timeout"])
    except (subprocess.TimeoutExpired, BaseException):
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait()
        raise
    finally:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    sys.exit(code if code >= 0 else 128 - code)
finally:
    for fd in fds:
        os.close(fd)
"""


def redact_output(value: str, bindings: list[dict]) -> str:
    secrets = set()
    for binding in bindings:
        raw = binding["value"]
        secrets.add(raw)
        if binding["format"] == "env":
            secrets.update(json.loads(raw).values())
    for secret in sorted(secrets, key=len, reverse=True):
        if not secret:
            continue
        for encoded in {secret, quote(secret, safe=""), base64.b64encode(secret.encode()).decode()}:
            value = value.replace(encoded, "[credential redacted]")
    return value[:100_000]


async def claim_command(service: ProductCredentialExecution, scope: RunScope, identity: str, bindings: list[dict]) -> None:
    await service.database.authorize_scope(scope)
    async with service.database.sessions.begin() as db:
        for binding in bindings:
            request, _ = await service.authorized_request(db, scope, binding["request_id"])
            if request.secret_revision != binding["revision"]:
                raise ToolFailed("A selected credential changed before dispatch.")
        row = await db.get(Effect, identity)
        if row is None or row.turn_id != scope.turn_id or row.actor_id != scope.actor_id or row.tool_name != "credentials_run":
            raise ToolFailed("This credential command has no durable effect reservation.")
        if row.state != "pending" or row.resolution is not None:
            raise ToolFailed("This credential command was already dispatched or settled. Do not repeat it.")
        claimed = await db.scalar(update(Effect).where(
            Effect.id == identity, Effect.state == "pending", Effect.resolution.is_(None),
        ).values(resolution=service.runtime.security.seal({"credential_phase": "dispatched"})).returning(Effect.id))
        if claimed is None:
            raise ToolFailed("Another worker already claimed this credential command.")


async def refuse_command(service: ProductCredentialExecution, scope: RunScope, identity: str) -> None:
    # This evidence-only transition can settle after revocation, but never clears
    # a dispatch marker or another turn's receipt.
    async with service.database.sessions.begin() as db:
        await db.execute(update(Effect).where(
            Effect.id == identity, Effect.turn_id == scope.turn_id, Effect.actor_id == scope.actor_id,
            Effect.tool_name == "credentials_run", Effect.state == "pending", Effect.resolution.is_(None),
        ).values(state="not_dispatched", settled_at=now()))


async def run_command(
    service: ProductCredentialExecution, ctx: RunContext[RunScope], args: RunCredential,
) -> dict:
    if not ctx.tool_call_id:
        raise ToolFailed("The credential command requires a native tool identity.")
    identity = effect_id(ctx, ToolCallPart("credentials_run", {}, tool_call_id=ctx.tool_call_id))
    try:
        bindings = await service.materialize(ctx.deps, args.request_ids)
        names = []
        for binding in bindings:
            names.extend(json.loads(binding["value"]).keys() if binding["format"] == "env" else [binding["env_var"]])
        if len(set(names)) != len(names):
            raise ToolFailed("Selected credentials contain overlapping environment variables.")
        reference = ctx.workspace.ref
        if reference is None or reference.provider != "modal":
            raise ToolFailed("Credential commands require this task's isolated Modal workspace.")
        await claim_command(service, ctx.deps, identity, bindings)
    except (HTTPException, PermissionError, ToolFailed):
        await refuse_command(service, ctx.deps, identity)
        raise ToolFailed(
            "Credential execution was refused before dispatch. request_ids must be IDs returned by "
            "credentials_request (pass a saved credential's id as secret_id there first); also check scope and workspace."
        ) from None
    try:
        result = await ctx.workspace.run(
            ["python3", "-I", "-c", COMMAND_GUEST],
            env={"MOYAI_NATIVE_CREDENTIAL_PAYLOAD": json.dumps({
                "bindings": bindings, "command": args.command, "timeout": args.timeout, "names": [],
            })},
            timeout=args.timeout + 10,
        )
    except Exception:  # noqa: BLE001 - provider errors may contain materialized values
        raise ToolFailed("The credential command outcome is uncertain. Inspect the task and reconcile its receipt; do not repeat it.") from None
    # Commit known completion before rechecking access or returning to Temporal.
    await confirm_effect(service.database, identity)
    await service.database.authorize_scope(ctx.deps)
    return {
        "exit_code": result.exit_code,
        "stdout": redact_output(result.stdout, bindings), "stderr": redact_output(result.stderr, bindings),
        "credentials": [{"request_id": item["request_id"], "revision": item["revision"]} for item in bindings],
    }


class CredentialTools(Toolset[RunScope]):
    def __init__(self, service: ProductCredentialExecution):
        self.service = service
        service.enabled = True
        tools: FunctionToolset[RunScope] = FunctionToolset(id=TOOLSET_ID)

        @tools.tool(metadata=READ)
        async def credentials_list(ctx: RunContext[RunScope], request: ListCredentials) -> dict:
            """Discover authorized saved access and pending requests; metadata never verifies capability.

            List without filters before concluding access is missing. Reuse a matching saved credential.
            Before answering whether a provider, model or integration works, check here for saved access; if it exists, verify with one small call (credentials_request, then credentials_run) instead of relying on docs or registries.
            """
            try:
                return await service.inventory(ctx.deps, request)
            except (HTTPException, PermissionError):
                raise ToolFailed("Credential metadata is unavailable for this requester and execution.") from None

        @tools.tool(metadata={**WRITE, "temporal": False}, sequential=True)
        async def credentials_request(ctx: RunContext[RunScope], request: CredentialRequest) -> dict:
            """Request task-specific access through a secure form, never through chat.

            Use this tool alone in its round. First list/reuse appropriate saved access.
            Declare exact named input_fields and accurate setup guidance, never credential values.
            Pending requests pause via the SDK; the user chooses sharing and session/persistent reuse.
            """
            result = await self.create_request(ctx.deps, ctx.run_id or "", ctx.tool_call_id or "", request)
            if result["status"] == "pending":
                raise CallDeferred(metadata={"kind": "credential_request", **result})
            return result

        @tools.tool(metadata=WRITE, sequential=True, description=(
            "Run one foreground command with approved generic credential handles. Values enter only that process; "
            "files use anonymous descriptors. Never print or copy credentials into ordinary files. "
            "Output is bounded/redacted; uncertain actions are not replayed. "
            "For saved access, first call credentials_request with secret_id set to the credential id from "
            "credentials_list; credentials_run.request_ids must use the IDs returned by credentials_request. "
            + ONEPASSWORD_GUIDANCE
        ))
        async def credentials_run(ctx: RunContext[RunScope], request: RunCredential) -> dict:
            """Run one foreground command with authorized generic credential handles.

            Values are injected into this process only; file credentials use anonymous descriptors.
            Never print credentials, write them to ordinary files, or detach a background process.
            Output is bounded/redacted. An uncertain command is never automatically replayed.
            For saved access, first call credentials_request with secret_id set to the credential id from
            credentials_list; credentials_run.request_ids must use the IDs returned by credentials_request.
            """
            return await run_command(service, ctx, request)

        super().__init__(tools, id=TOOLSET_ID)

    @durable_operation("request")
    async def create_request(
        self, scope: RunScope, run_id: str, tool_call_id: str, request: CredentialRequest,
    ) -> dict[str, Any]:
        try:
            return await self.service.request(scope, run_id, tool_call_id, request)
        except (HTTPException, PermissionError):
            raise ToolFailed("This secure credential request is unavailable or changed. Check the current task and saved access.") from None


def capability(service: ProductCredentialExecution) -> CredentialTools:
    return CredentialTools(service)
