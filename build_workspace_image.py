"""Prewarm the same Modal computer image used by the Pydantic runtime."""
import asyncio

import modal
from moyai.agent import workspace_image
from moyai.settings import Settings


async def build_workspace_image(settings: Settings) -> modal.Image:
    application = await modal.App.lookup.aio(settings.modal_app, create_if_missing=True)
    image = workspace_image()
    await image.build.aio(application)
    return image


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(override=False)
    with modal.enable_output():
        image = asyncio.run(build_workspace_image(Settings()))
    print(f"Workspace image ready: {image.object_id}", flush=True)


if __name__ == "__main__":
    main()
