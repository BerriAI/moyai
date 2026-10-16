"""Pydantic workspace capability for AWS Lambda MicroVMs."""
from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    WorkspaceBackend,
    WorkspaceCommand,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.protocol import validate_timeout

from .contracts import RunScope
from .lambda_control import LambdaControl, LambdaVM, ObservationError
from .settings import Settings

logger = logging.getLogger(__name__)

class LambdaSandbox(AbstractCapability[RunScope]):
    def __init__(self, settings: Settings):
        self.settings = settings

    def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        if ref is not None and ref.provider != "lambda":
            return None
        return LambdaBackend(self.settings, ref=ref, name=f"native:{ctx.deps.workspace_key}:{ctx.run_id}")

    async def destroy(self, ref: WorkspaceRef) -> None:
        if ref.provider != "lambda":
            raise ValueError("Expected a Lambda workspace")
        try:
            await (await LambdaControl(self.settings).get(ref.id)).terminate()
        except WorkspaceUnavailableError:
            pass


class WorkspaceProviders(AbstractCapability[RunScope]):
    """The configured default applies only to new workspaces; refs keep their provider."""
    def __init__(self, settings: Settings, modal: AbstractCapability[RunScope]):
        self.settings, self.modal = settings, modal
        self.lambda_sandbox = LambdaSandbox(settings)

    def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None) -> WorkspaceBackend | None:
        provider = ref.provider if ref else self.settings.sandbox_provider
        selected = self.lambda_sandbox if provider == "lambda" else self.modal
        return selected.get_workspace(ctx, ref=ref)


class LambdaProductComputer:
    """Current product file-import/Git preparation adapter over the native backend."""
    provider = "lambda"

    def __init__(self, settings: Settings, identity: str):
        self.object_id = identity
        self.backend = LambdaBackend(settings, ref=WorkspaceRef(provider="lambda", id=identity))
        self.exec = SimpleNamespace(aio=self.execute)
        self.filesystem = SimpleNamespace(
            write_bytes=SimpleNamespace(aio=self.write_bytes),
            copy_from_local=SimpleNamespace(aio=self.copy_from_local),
        )

    async def execute(self, *command: str, timeout: float | None = None) -> Any:
        result = await self.backend.run(command, timeout=timeout)
        async def wait():
            return result.exit_code
        async def stdout():
            return result.stdout
        async def stderr():
            return result.stderr
        return SimpleNamespace(
            returncode=result.exit_code, wait=SimpleNamespace(aio=wait),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=stdout)),
            stderr=SimpleNamespace(read=SimpleNamespace(aio=stderr)),
        )

    async def _write(self, path: str, raw: bytes, *, append: bool) -> None:
        result = await self.backend.run([
            "python3", "-I", "-c",
            ("import base64,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
             "p.open(sys.argv[2]).write(base64.b64decode(sys.argv[3]))"),
            path, "ab" if append else "wb", base64.b64encode(raw).decode(),
        ], timeout=30)
        if result.exit_code:
            raise RuntimeError("Native workspace file transfer failed")

    async def write_bytes(self, raw: bytes, path: str) -> None:
        for offset in range(0, max(1, len(raw)), 128 * 1024):
            await self._write(path, raw[offset:offset + 128 * 1024], append=offset > 0)

    async def copy_from_local(self, source: Path, destination: str) -> None:
        with source.open("rb") as stream:
            append = False
            while chunk := stream.read(128 * 1024):
                await self._write(destination, chunk, append=append)
                append = True
            if not append:
                await self._write(destination, b"", append=False)


class LambdaBackend:
    """Commands supply the SDK's built-in filesystem fallback on the same VM."""
    def __init__(self, settings: Settings, *, ref: WorkspaceRef | None = None, name: str | None = None):
        if ref is not None and ref.provider != "lambda":
            raise ValueError("Expected a Lambda workspace")
        self.control = LambdaControl(settings)
        self._ref = ref
        self.name = name or "native:" + uuid4().hex
        self.vm: LambdaVM | None = None
        self.lock = asyncio.Lock()

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    async def _get(self) -> LambdaVM:
        async with self.lock:
            if self.vm is not None:
                return self.vm
            if self._ref is not None:
                self.vm = await self.control.get(self._ref.id)
                await self.vm.running()
                return self.vm
            def created(vm: LambdaVM) -> None:
                self._ref = WorkspaceRef(provider="lambda", id=vm.id)
            task = asyncio.create_task(self.control.create(self.name, created=created))
            try:
                self.vm = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Finish recording the ref even when cancellation races RunMicrovm.
                # Application lifecycle code owns this VM; a run never destroys it.
                try:
                    self.vm = await task
                finally:
                    raise
            assert self.vm is not None
            return self.vm

    async def working_dir(self) -> str:
        await self._get()
        return "/workspace"

    async def run(
        self, command: WorkspaceCommand, *, shell: bool = False,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> CommandResult:
        validate_timeout(timeout)
        if isinstance(command, str):
            if not shell:
                raise TypeError("String commands require shell=True")
            argv = ["/bin/sh", "-c", command]
        else:
            if shell:
                raise TypeError("Argument sequences require shell=False")
            argv = list(command)
            if not argv or any(not isinstance(arg, str) for arg in argv):
                raise ValueError("Command must be a nonempty string sequence")
        vm = await self._get()
        identity = uuid4().hex
        output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        async with vm.lock:
            try:
                try:
                    await vm.request("/command", {
                        "id": identity, "command": argv, "env": dict(env or {}), "timeout": timeout,
                    })
                except (ObservationError, httpx.TransportError):
                    pass  # Resolve a lost ACK by observation, never resubmission.
                while True:
                    try:
                        result = await vm.request("/command/read", {
                            "id": identity, **{key: len(value) for key, value in output.items()},
                        })
                    except (ObservationError, httpx.TransportError):
                        if not await vm.alive():
                            raise WorkspaceUnavailableError("Lambda workspace terminated") from None
                        raise
                    for stream, value in output.items():
                        value.extend(base64.b64decode(result[stream]))
                    text: dict[str, Any] = {
                        key: value.decode("utf-8", "replace") for key, value in output.items()
                    }
                    if sum(map(len, output.values())) > 32 * 1024 * 1024:
                        raise WorkspaceOutputLimitError("Command output exceeds 32 MiB", limit=32*1024*1024, **text)
                    if result["state"] == "done":
                        # Read through EOF; the guest uses regular spool files,
                        # so a detached child cannot hold a pipe open forever.
                        if any(len(base64.b64decode(result[key])) == 128 * 1024 for key in output):
                            continue
                        if result.get("timed_out"):
                            raise WorkspaceTimeoutError("Lambda command timed out", **text)
                        return CommandResult(exit_code=result["exit_code"], **text)
                    await asyncio.sleep(.15)
            except BaseException:
                try:
                    await asyncio.shield(vm.request("/command/stop", {"id": identity}))
                except (RuntimeError, httpx.TransportError, OSError):
                    logger.warning("Could not confirm Lambda command cancellation")
                raise
