"""Explicit requests to move a pinned agent at its next settled checkpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass

from temporalio.common import WorkerDeploymentVersion

VERSION = "moyai-deployment-checkpoint-v1"


@dataclass(frozen=True)
class DeploymentHandoff:
    """The operator selects eligible destinations through Temporal routing.

    The source identity prevents a delayed request from moving a later release.
    A signal requests a checkpoint; it never authorizes an external action or
    changes the deployment's routing/compatibility policy.
    """

    request_id: str
    deployment_name: str
    source_build_id: str

    def applies_to(self, version: WorkerDeploymentVersion | None) -> bool:
        return (
            version is not None
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,199}", self.request_id))
            and self.deployment_name == version.deployment_name
            and self.source_build_id == version.build_id
        )
