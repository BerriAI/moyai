"""Build and validate the agent image before deploying the web service."""

import asyncio

import modal

from app.config import Settings
from app.modal_clients import ModalClients
from app.workspace_image import workspace_image, agent_runtime_image


async def build_workspace_image(settings: Settings) -> modal.Image:
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise ValueError(
            "Configure MODAL_TOKEN_ID and MODAL_TOKEN_SECRET before building the workspace image."
        )
    clients = ModalClients()
    try:
        client = await clients.get(settings)
        app = await modal.App.lookup.aio(
            settings.modal_app_name, create_if_missing=True, client=client
        )
        image = workspace_image(settings)
        builds = [asyncio.create_task(image.build.aio(app))]
        if settings.independent_agent_runtime:
            builds.append(asyncio.create_task(agent_runtime_image().build.aio(app)))
        try:
            await asyncio.gather(*builds)
        finally:
            for task in builds:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*builds, return_exceptions=True)
        return image
    finally:
        await clients.close()


def main() -> None:
    settings = Settings()
    if settings.sandbox_provider == "substrate":
        print(
            "Substrate uses its configured OCI runtime image; verify it in Settings → Runtime."
        )
        return
    if settings.missing_sandbox("modal"):
        print(
            "Configure the sandbox connection in Settings → Runtime to build the agent image."
        )
        return
    print(
        "Building and validating the Modal workspace image before deployment...",
        flush=True,
    )
    with modal.enable_output():
        image = asyncio.run(build_workspace_image(settings))
    print(f"Workspace image ready: {image.object_id}", flush=True)


if __name__ == "__main__":
    main()
