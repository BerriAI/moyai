"""Build the real image and check a short-lived Modal sandbox, without model calls.

Run with the target workspace's Modal credentials in .env or the environment:
    uv run python scripts/workspace_image_smoke.py
The app and sandbox are temporary; no web service is deployed or restarted.
"""
import asyncio
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import modal

from app.config import Settings
from app.workspace_image import workspace_image


async def main() -> None:
    settings = Settings()
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise ValueError('Configure MODAL_TOKEN_ID and MODAL_TOKEN_SECRET for this verification.')
    client = await modal.Client.from_credentials.aio(settings.modal_token_id, settings.modal_token_secret)
    async with modal.App('moyai-workspace-image-verification').run.aio(client=client) as app:
        started = time.monotonic()
        image = workspace_image(settings)
        await image.build.aio(app)
        print(f'Image build passed: {image.object_id} ({time.monotonic() - started:.1f}s)', flush=True)
        sandbox = await modal.Sandbox.create.aio(
            'bash', '-lc', 'cd /opt/hermes && /opt/hermes-env/bin/python -c '
            "\"from run_agent import AIAgent; import pip, claude_agent_sdk; print('Hermes and Claude SDK imports passed')\" && "
            '/opt/hermes-env/bin/python /opt/workspace-runner/sandbox/harness_dependencies.py && '
            'codex --version && opencode --version && pi --version && echo WORKSPACE_READY',
            image=image, app=app, client=client, timeout=120, cpu=2, memory=4096)
        try:
            await sandbox.wait.aio()
            print(await sandbox.stdout.read.aio(), flush=True)
            print(await sandbox.stderr.read.aio(), flush=True)
            if await sandbox.poll.aio() != 0:
                raise RuntimeError('Workspace smoke test failed; see sandbox output above.')
        finally:
            await sandbox.terminate.aio()


if __name__ == '__main__':
    with modal.enable_output():
        asyncio.run(main())
