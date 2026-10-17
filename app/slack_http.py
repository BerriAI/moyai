"""Application HTTP boundaries for AgentChat's Slack services."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager

import httpx
from agentchat.channels.slack.delivery import SlackDelivery
from agentchat.channels.slack.webhook import SlackBodyTooLarge, SlackPayloadError, SlackSignatureError
from fastapi import HTTPException


@contextmanager
def webhook_errors():
    """Translate SDK errors into the application's public HTTP contract."""
    try:
        yield
    except SlackSignatureError:
        raise HTTPException(401, "Invalid Slack request.") from None
    except SlackBodyTooLarge:
        raise HTTPException(413, "Slack payload is too large.") from None
    except SlackPayloadError:
        raise HTTPException(400, "Invalid Slack payload.") from None


class SlackHTTP:
    """Run SDK requests using the host's authenticated, managed HTTP client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str] | None = None,
        *,
        timeout: float | None = None,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.client = client
        self.headers = headers
        self.timeout = client.timeout if timeout is None else timeout
        self.authorize = authorize
        self.delivery = SlackDelivery(self.request, transport_errors=(httpx.HTTPError, ValueError))

    async def request(self, verb: str, method: str, payload: dict) -> dict:
        if self.authorize is not None:
            await self.authorize()
        response = await self.client.request(
            verb,
            method,
            headers=self.headers,
            timeout=self.timeout,
            **({"params": payload} if verb == "GET" else {"json": payload}),
        )
        response.raise_for_status()
        return response.json()
