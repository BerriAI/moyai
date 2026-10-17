"""Keep the deployment workflow identity separate from its Moyai checkout."""
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.release.errors import ReleaseError

SOURCE_REPOSITORY = 'BerriAI/moyai'
DEPLOYMENT_REPOSITORY = 'BerriAI/moyai-ops'


@dataclass(frozen=True)
class DeploymentContext:
    repository: str
    workflow_sha: str
    run_id: str
    source_sha: str

    @classmethod
    def from_environment(cls, source_sha=None):
        repository = os.environ.get('GITHUB_REPOSITORY', '')
        if (repository not in {SOURCE_REPOSITORY, DEPLOYMENT_REPOSITORY}
                or os.environ.get('GITHUB_REF') != 'refs/heads/main'
                or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
            raise ReleaseError('Production deployment requires a main workflow dispatch in BerriAI/moyai or BerriAI/moyai-ops.')
        workflow_sha = os.environ.get('GITHUB_SHA', '')
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        if not re.fullmatch('[0-9a-f]{40}', workflow_sha) or not re.fullmatch('[1-9][0-9]*', run_id):
            raise ReleaseError('The deployment workflow identity is incomplete; no changes made.')
        if repository == SOURCE_REPOSITORY:
            if source_sha is not None and source_sha != workflow_sha:
                raise ReleaseError('A Moyai workflow must deploy its own dispatched commit.')
            source_sha = workflow_sha
        if not isinstance(source_sha, str) or not re.fullmatch('[0-9a-f]{40}', source_sha):
            raise ReleaseError('The private deployment workflow requires an explicit full Moyai source SHA.')
        return cls(repository, workflow_sha, run_id, source_sha)

    def validate_checkout(self, checkout: Path):
        """Release tooling and candidate imports must come from one clean SHA."""
        def git(*arguments):
            result = subprocess.run(['git', '-C', str(checkout), *arguments],
                check=False, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError()
            return result.stdout.strip()
        try:
            if (Path(git('rev-parse', '--show-toplevel')).resolve() != checkout.resolve()
                    or git('rev-parse', 'HEAD') != self.source_sha
                    or git('status', '--porcelain', '--untracked-files=normal')):
                raise ValueError()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ReleaseError('Release tooling must run from a clean Moyai checkout at the selected source SHA; no changes made.') from None

    def receipt(self):
        return {
            'source': {'repository': SOURCE_REPOSITORY, 'sha': self.source_sha},
            'workflow': {'repository': self.repository, 'sha': self.workflow_sha, 'run_id': self.run_id},
        }
