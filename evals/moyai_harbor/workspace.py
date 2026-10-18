from __future__ import annotations

import math
import shlex
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from harbor.environments.base import BaseEnvironment
from moyai import workspace_recovery
from moyai.contracts import RunScope
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    Workspace,
    WorkspaceBackend,
    WorkspaceRef,
)


class HarborCommandBackend(SupportsCommands):
    def __init__(self, environment: BaseEnvironment, ref: WorkspaceRef) -> None:
        self.environment = environment
        self._ref = ref

    @property
    def ref(self) -> WorkspaceRef:
        return self._ref

    async def working_dir(self) -> str:
        return "/workspace"

    async def run(
        self,
        command: str | Sequence[str],
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if isinstance(command, str):
            if not shell:
                raise TypeError("String commands require shell=True.")
            rendered = command
        else:
            if shell:
                raise TypeError("Argv commands require shell=False.")
            rendered = shlex.join(command)
        result = await self.environment.exec(
            rendered,
            cwd="/workspace",
            env=dict(env or {}),
            timeout_sec=math.ceil(timeout) if timeout is not None else None,
        )
        return CommandResult(
            exit_code=result.return_code,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


class HarborWorkspace(AbstractCapability[RunScope]):
    id = "moyai_harbor_workspace"

    def __init__(self, environment: BaseEnvironment) -> None:
        self.environment = environment
        self.reference = WorkspaceRef(
            provider="modal",
            id=f"harbor-eval-{environment.session_id}",
        )

    @contextmanager
    def runtime_context(self, database: Any) -> Iterator[None]:
        """Route only this eval computer's lifecycle checks back to Harbor.

        The Modal-shaped reference exists for credential execution. It is not
        a Modal sandbox, and a running Docker computer is not a durable snapshot.
        """
        original_live = workspace_recovery.live_sandbox
        original_snapshots = database.answers.snapshots
        expected = {"provider": self.reference.provider, "id": self.reference.id}
        workspace = self

        async def live_sandbox(reference: dict) -> bool:
            if reference != expected:
                return await original_live(reference)
            result = await self.environment.exec("true", cwd="/workspace", timeout_sec=5)
            return result.return_code == 0

        class EvalSnapshots:
            async def capture(self, reference: WorkspaceRef, *, require_live: bool) -> str:
                if reference == workspace.reference:
                    raise RuntimeError(
                        "Local eval computers stay available while the server runs; durable snapshots are unavailable."
                    )
                return await original_snapshots.capture(reference, require_live=require_live)

        database.answers.snapshots = EvalSnapshots()
        try:
            with patch("moyai.workspace_recovery.live_sandbox", live_sandbox):
                yield
        finally:
            database.answers.snapshots = original_snapshots

    def get_workspace(
        self,
        _ctx: RunContext[RunScope],
        *,
        ref: WorkspaceRef | None,
    ) -> WorkspaceBackend | None:
        if ref is not None and ref != self.reference:
            return None
        # Eval-only: credentials_run requires the isolated Modal workspace provider.
        return Workspace(HarborCommandBackend(self.environment, self.reference))
