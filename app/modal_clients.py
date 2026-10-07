"""Application-owned Modal connections, shared across repeated workspace checks."""
import asyncio

import modal

from .config import Settings


class ModalClients:
    def __init__(self):
        self._clients: dict[tuple[str, str], asyncio.Task[modal.Client]] = {}
        self._closed = False

    async def get(self, settings: Settings) -> modal.Client:
        if self._closed:
            raise RuntimeError('Modal connections are shutting down.')
        credentials = (settings.modal_token_id, settings.modal_token_secret)
        task = self._clients.get(credentials)
        if task is None:
            # Publish before awaiting: concurrent callers share initialization.
            # The SDK retains every factory-created client until process exit.
            task = asyncio.create_task(modal.Client.from_credentials.aio(*credentials))
            self._clients[credentials] = task

            def completed(result: asyncio.Task[modal.Client]) -> None:
                if result.cancelled() or result.exception() is not None:
                    if self._clients.get(credentials) is result:
                        self._clients.pop(credentials)

            task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients = await asyncio.gather(*self._clients.values(), return_exceptions=True)
        self._clients.clear()
        # from_credentials already entered the SDK client. Do not enter twice.
        await asyncio.gather(*(client.__aexit__(None, None, None)
                               for client in clients if not isinstance(client, BaseException)))
