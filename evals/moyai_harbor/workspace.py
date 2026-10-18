from __future__ import annotations

import math
import shlex
from collections.abc import Mapping, Sequence

from harbor.environments.base import BaseEnvironment
from moyai.contracts import RunScope
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    CommandResult,
    SupportsCommands,
    Workspace,
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

    def get_workspace(
        self,
        ctx: RunContext[RunScope],
        *,
        ref: WorkspaceRef | None,
    ) -> Workspace | None:
        if ref is not None and ref != self.reference:
            return None
        # Eval-only: credentials_run requires the isolated Modal workspace provider.
        return Workspace(HarborCommandBackend(self.environment, self.reference))
