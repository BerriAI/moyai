"""Build and validate the agent image before deploying the web service."""
import asyncio

import modal

from app.config import Settings
from app.workspace_image import workspace_image


async def build_workspace_image(settings: Settings) -> modal.Image:
    if not settings.modal_token_id or not settings.modal_token_secret:
        raise ValueError('Configure MODAL_TOKEN_ID and MODAL_TOKEN_SECRET before building the workspace image.')
    client = await modal.Client.from_credentials.aio(settings.modal_token_id, settings.modal_token_secret)
    app = await modal.App.lookup.aio(settings.modal_app_name, create_if_missing=True, client=client)
    image = workspace_image(settings)
    await image.build.aio(app)
    return image


def main() -> None:
    print('Building and validating the Modal workspace image before deployment...', flush=True)
    with modal.enable_output():
        image = asyncio.run(build_workspace_image(Settings()))
    print(f'Workspace image ready: {image.object_id}', flush=True)


if __name__ == '__main__':
    main()
