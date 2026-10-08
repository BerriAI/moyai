"""Keep the Modal implementation and native handles behind a provider boundary."""
import hashlib

import modal

from ..workspace_image import workspace_image
from ..modal_clients import ModalClients


def sandbox_name(name: str) -> str:
    # Preserve existing identities; creation and lost-ACK lookup must agree.
    return name if len(name) <= 64 else hashlib.sha256(name.encode()).hexdigest()


class ModalProvider:
    name = 'modal'

    def __init__(self, settings, clients=None):
        self.settings = settings
        self.clients = clients if clients is not None else ModalClients()
        self.image = lambda: workspace_image(settings)

    async def client(self):
        return await self.clients.get(self.settings)

    async def get(self, identity):
        return await modal.Sandbox.from_id.aio(identity, client=await self.client())

    async def find(self, name, *, initialize=False, token='', timeout=86400, apt_packages=()):
        return await modal.Sandbox.from_name.aio(self.settings.modal_app_name, sandbox_name(name), client=await self.client())

    async def create(self, *, name=None, snapshot_id='', token='', timeout=86400, memory=4096, apt_packages=()):
        client = await self.client()
        app = await modal.App.lookup.aio(self.settings.modal_app_name, create_if_missing=True, client=client)
        image = modal.Image.from_id(snapshot_id, client=client) if snapshot_id else self.image()
        if apt_packages:
            image = image.apt_install(*apt_packages)
        return await modal.Sandbox.create.aio(
            **({'name': sandbox_name(name)} if name else {}), app=app, client=client, image=image,
            secrets=[modal.Secret.from_dict({'WORKSPACE_RUN_TOKEN': token})] if token else [],
            env={'PYTHONUNBUFFERED': '1', 'PYTHONPATH': '/opt/hermes', 'HERMES_HOME': '/tmp/hermes-home',
                 'HERMES_RUNTIME_DIR': '/opt/hermes-tools', 'HERMES_PYTHON': '/opt/hermes-env/bin/python',
                 'GIT_TERMINAL_PROMPT': '0'}, timeout=timeout, cpu=2, memory=memory,
            experimental_options={'vm_runtime': True} if self.settings.modal_vm_runtime else {})

    async def check(self):
        # Authenticate without starting billable compute or building an image.
        await modal.App.lookup.aio(self.settings.modal_app_name, create_if_missing=True, client=await self.client())
        return 'Connected to Modal.'
