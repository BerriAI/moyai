"""Broker-owned sockets with request-local HTTP state and explicit shutdown."""

from __future__ import annotations

import httpx2 as httpx

from .inference_usage import INFERENCE_ACTIVITY, InferenceActivity, InferenceUsageTransport


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, owner: BrokerUpstream):
        self.owner = owner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.owner._require_open(INFERENCE_ACTIVITY.get())
        return await self.owner.transport.handle_async_request(request)

    async def aclose(self) -> None:
        # A request owns its response stream, never the process's socket pool.
        pass


class BrokerUpstream:
    """One bounded pool per running broker, never shared across event loops.

    Fresh clients keep cookies, credentials and timeout settings request-local.
    Admission/usage capture still runs for every physical request. No retries or
    redirects are introduced. Unlimited product slots use a finite socket cap.
    """

    def __init__(self, capacity: int, *, transport: httpx.AsyncBaseTransport | None = None):
        maximum = capacity or 100
        self.transport = InferenceUsageTransport(
            transport if transport is not None else httpx.AsyncHTTPTransport(
                limits=httpx.Limits(
                    max_connections=maximum, max_keepalive_connections=min(maximum, 20),
                    keepalive_expiry=30.0,
                ),
                retries=0,
            ),
            require_admission=True,
        )
        self.closed = False

    def _require_open(self, activity: InferenceActivity | None = None) -> None:
        if self.closed:
            from .product_inference import InferenceAdmissionError

            # This guard runs before admission and any possible upstream byte.
            # A borrowed client's late send must retain that classification even
            # though it never reaches InferenceUsageTransport's admission hook.
            error = InferenceAdmissionError("Product inference admission is not running.")
            if activity is not None:
                activity.admission_error = error
            raise error

    def client(self, timeout: float) -> httpx.AsyncClient:
        # Client construction must not mark an unrelated ambient activity.
        self._require_open()
        return httpx.AsyncClient(
            transport=_BorrowedTransport(self), timeout=timeout, follow_redirects=False,
        )

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            await self.transport.aclose()
