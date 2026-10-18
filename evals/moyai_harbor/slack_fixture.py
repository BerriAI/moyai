"""Keep synthetic Slack traffic local while preserving other HTTP destinations."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx


@contextmanager
def route_slack_clients(transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    original_init = httpx.AsyncClient.__init__

    def routed_init(client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        # A hostname-specific mount preserves the client's normal transport,
        # proxy settings and caller-provided mounts for the model gateway and
        # every non-Slack destination. It also keeps auth/event handling intact.
        mounts = dict(kwargs.get("mounts") or {})
        mounts["all://slack.com"] = transport
        kwargs["mounts"] = mounts
        original_init(client, *args, **kwargs)

    # Preserve the class identity: SDKs such as OpenAI validate supplied clients
    # with isinstance(..., httpx.AsyncClient), including during delayed setup.
    with patch.object(httpx.AsyncClient, "__init__", routed_init):
        yield
