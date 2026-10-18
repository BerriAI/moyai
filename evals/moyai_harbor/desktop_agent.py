from __future__ import annotations

import os

from harbor.environments.base import BaseEnvironment

from moyai_harbor.agent import MoyaiAgent


class MoyaiDesktopAgent(MoyaiAgent):
    """Moyai on a Linux desktop. MOYAI_EVAL_OCU=0 removes open-computer-use for the baseline arm."""

    @staticmethod
    def name() -> str:
        return "moyai-desktop-eval"

    async def setup(self, environment: BaseEnvironment) -> None:
        await environment.exec(f"mkdir -p {self.environment_logs_dir}", cwd="/workspace")
        if os.environ.get("MOYAI_EVAL_OCU", "1") != "1":
            removed = await environment.exec(
                "rm -rf /opt/moyai/tools/node_modules; "
                "rm -f /usr/local/bin/ocu /usr/local/bin/open-computer-use; "
                "! command -v ocu && ! command -v open-computer-use",
                cwd="/workspace",
            )
            if removed.return_code != 0:
                raise RuntimeError("Could not remove open-computer-use for the baseline arm.")
        started = await environment.exec("sh /eval/start_desktop.sh", cwd="/workspace", timeout_sec=120)
        if started.return_code != 0:
            raise RuntimeError(f"The eval desktop did not start: {started.stderr or started.stdout}")
