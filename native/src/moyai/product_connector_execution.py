"""Execute canonical connectors with invocation-local helpers and current authority."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.connector_errors import ConnectorError
from app.connectors import TOOLS
from app.db import database as source_database
from app.personal_slack import Selection
from pydantic_ai.exceptions import ToolFailed

from .contracts import Grant, RunScope

if TYPE_CHECKING:
    from .product_connections import ProductConnections


class _Invocation:
    def __init__(
        self, connectors: Any, authorize: Callable[[], Awaitable[None]] | None, *, recheck: bool,
    ):
        self.connectors, self.authorize, self.recheck = connectors, authorize, recheck
        self.loop = asyncio.get_running_loop()

    async def check(self) -> None:
        authorize = self.authorize
        if authorize is not None:
            async def verify() -> None:
                await authorize()

            # Native SQL resources stay on their owning loop, even while the
            # canonical helper runs synchronous Store operations in a thread.
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(verify(), self.loop))

    def guarded(self, request: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            await self.check()
            result = await request(*args, **kwargs)
            if self.recheck:
                await self.check()
            return result
        return invoke

    def run[T](self, operation: Callable[[Any], Awaitable[T]]) -> T:
        bound = copy.copy(self.connectors)
        bound.locks = {provider: asyncio.Lock() for provider in bound.locks}
        bound.personal_slack = copy.copy(bound.personal_slack)
        bound.personal_slack.connectors, bound.personal_slack.locks = bound, {}
        bound.github = copy.copy(bound.github)
        bound.github.connectors = bound
        for key, value in vars(bound.github).items():
            if isinstance(value, asyncio.Lock):
                setattr(bound.github, key, asyncio.Lock())
        if self.authorize is not None:
            bound.request = self.guarded(bound.request)
            bound.github.request = self.guarded(bound.github.request)

        async def execute() -> T:
            return await operation(bound)

        return asyncio.run(execute())


class CanonicalConnectorExecution:
    def __init__(self, connections: ProductConnections):
        self.connections = connections

    async def call[T](
        self, operation: Callable[[Any], Awaitable[T]], *,
        authorize: Callable[[], Awaitable[None]] | None = None, recheck: bool = False,
    ) -> T:
        """Run a canonical helper off-loop, guarding each request when scoped.

        Reads may require a result recheck; writes must retain received evidence
        after revocation. Unscoped account/setup calls retain their own admission.
        Each invocation owns its locks; SQL CAS remains the cross-process fence.
        Cancellation drains the helper before propagating to its caller.
        """
        invocation = _Invocation(self.connections.connectors, authorize, recheck=recheck)
        return await source_database(invocation.run, operation)

    async def tool(self, scope: RunScope, grant: Grant, name: str, arguments: dict[str, Any]) -> Any:
        """Execute a selected canonical tool without exposing helper rebinding."""
        service = self.connections
        _, write, model, _ = TOOLS[name]
        args = model.model_validate(arguments).model_dump()
        if grant.policy["product"].get("access") == "bot" and name in {"slack_channel", "slack_thread"}:
            args["as_bot"] = True
        bot_only = grant.kind == "slack" and (name in {"slack_me", "slack_send"} or args.get("as_bot") is True)
        if bot_only:
            await service.check(scope, grant)
            if grant.policy["product"]["personal_owner"]:
                raise ToolFailed("Bot access requires the organization Slack account selection.")
            if name in {"slack_thread", "slack_channel"}:
                await service.require_bot_dm(scope, args["channel"])
            credentials = {}
            bot_token = await service.bot_token(scope, grant)
        else:
            credentials = await service.credentials(scope, grant, write=write)
            bot_token = None
        invocation = _Invocation(service.connectors, lambda: service.check(scope, grant), recheck=not write)

        async def operate(bound: Any) -> Any:
            async def selected_credentials(_provider: str):
                await invocation.check()
                return credentials

            async def selected_slack(*_args: Any, **_kwargs: Any):
                return Selection(grant.policy["product"]["personal_owner"], None, None, name, credentials)

            async def selected_bot_token():
                await invocation.check()
                if bot_token is None:
                    raise ToolFailed("The bot account was not selected for this operation.")
                return bot_token

            # Native current-scope checks replace the old run-bound policy;
            # no fabricated source execution or legacy authority is introduced.
            bound.credentials = selected_credentials
            if bot_only:
                bound.slack_bot_token = selected_bot_token
            bound.allowed = lambda selected_name, **_kwargs: (
                selected_name in TOOLS and TOOLS[selected_name][0] == grant.kind
                and (not TOOLS[selected_name][1] or not grant.read_only)
            )
            bound.check_execution = lambda _run: None
            bound.guard = lambda *_args, **_kwargs: None
            bound.personal_slack.select = selected_slack
            bound.personal_slack.recheck = lambda _selection: None
            bound.slack_identities = copy.copy(service.identities)
            bound.slack_identities.connectors = bound
            bound.slack_identities.check_requester = lambda *_args, **_kwargs: None
            try:
                if name == "linear_my_issues":
                    return await bound.my_linear_issues({"active_user_id": scope.actor_id}, args)
                if grant.kind == "slack" and args.get("as_bot"):
                    await invocation.check()
                result = await bound.call(
                    name, args, run={"id": scope.session_id, "active_user_id": scope.actor_id}
                    if name in {"slack_me", "slack_send"} else None,
                )
            except ConnectorError as error:
                # Raising retains EffectReceipts' uncertain-write barrier.
                raise ToolFailed(str(error)) from None
            if write and isinstance(result, dict) and (
                result.get("outcome_uncertain") or result.get("error") or result.get("complete") is False
            ):
                raise ToolFailed("The provider did not confirm the complete action. Verify before retrying.")
            return result

        return await source_database(invocation.run, operate)
