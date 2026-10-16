"""Live canonical account grants and onboarding for the native core.

Runtime owns construction: ``ProductConnections(runtime.product, runtime.database)``.
Database.resolve_grants delegates here in product mode. Grants contain references,
never tokens; calls re-resolve credentials and authority on the trusted worker.
The source Connectors service has no dependency on an old execution engine.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakValueDictionary

from app.db import database as source_database
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .contracts import Grant, RunScope

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .product_resources import ProductResources

Provider = Literal["github", "linear", "notion", "slack"]
PROVIDERS = ("github", "linear", "notion", "slack")


class ProductConnections:
    def __init__(self, product: ProductResources, database: Database):
        from app.connectors import Connectors
        from app.identities import SlackIdentities
        from app.requester_identity import same_requester

        self.product, self.database = product, database
        self.store, self.security, self.settings = product.store_proxy, product.security, product.settings
        self.connectors = Connectors(cast(Any, self.store), self.security, self.settings)
        self.identities = SlackIdentities(
            self.store, self.connectors, self.settings, self.security, CommittedSourceWrites(),
            lambda owner, actor: same_requester(self.store, owner, actor),
        )
        cast(Any, self.connectors).slack_identities = self.identities
        self._public_repositories: dict[int, tuple[float, dict[str, Any]]] = {}
        self._public_repository_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        if getattr(database, "execution_schema", None):
            cast(Any, self.connectors.github).source_run_mutable = self.source_run_mutable

    def source_run_mutable(self, connection: Any, run_id: str) -> bool:
        native = self.database.execution_schema
        source = self.product.identity.schema
        if not native or not source or not all(re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) for value in (native, source)):
            raise ValueError("Explicit source and native schemas are required.")
        # The canonical helper holds the same source writer lock as continuation
        # admission. A rename cannot race a handoff or rewrite frozen ancestry.
        moved = connection.execute(
            f"""SELECT 1 FROM "{native}".moyai_product_continuations moved
                WHERE moved.source_schema=? AND (moved.source_session_id=?
                  OR moved.source_session_id IN (SELECT root_id FROM "{source}".run_roots WHERE run_id=?))
                LIMIT 1""", (source, run_id, run_id),
        ).fetchone()
        return moved is None

    def _grants(
        self, actor_id: str, *, shared_session: bool, surface: str,
        source_store: Any = None, source_connectors: Any = None,
    ) -> list[Grant]:
        from app.connection_authority import authority
        from app.connector_errors import ConnectorError

        store = source_store if source_store is not None else self.store
        connectors = source_connectors if source_connectors is not None else self.connectors
        result: list[Grant] = []
        for provider in PROVIDERS:
            policy = connectors.policy(provider)
            if not policy["enabled"]:
                continue
            rows = store.rows("SELECT * FROM connections WHERE provider=?", (provider,))
            personal = connectors.personal_slack
            owner = personal.owner(actor_id) if provider == "slack" else None
            personal_row = personal.row(owner) if owner else None
            selected: list[tuple[Any, str | None, str]] = []
            if personal_row and personal_row["encrypted"] is not None:
                # Personal selection is authoritative. Its absence from this
                # surface never exposes the organization's search account.
                if not shared_session and surface == "web" and owner == actor_id:
                    selected.append((personal_row, owner, "personal_reads"))
                if rows:
                    organization = json.loads(self.security.decrypt(rows[0]["encrypted"]))
                    if (organization.get("bot") or {}).get("access_token"):
                        selected.append((rows[0], None, "bot"))
            elif rows:
                selected.append((rows[0], None, "organization"))
            audit = store.rows(
                "SELECT MAX(id) AS revision FROM connection_audit WHERE provider=?", (provider,),
            )[0]["revision"]
            for row, owner, access in selected:
                credentials = json.loads(self.security.decrypt(row["encrypted"]))
                scopes: list[int | None] = [None]
                if provider == "github":
                    try:
                        scopes = connectors.github.connected_ids(credentials)
                    except ConnectorError:
                        # Legacy names must go through the existing explicit
                        # metadata migration; never do provider I/O under an
                        # admission transaction or break unrelated accounts.
                        continue
                app_version = connectors.github.connection_version() if provider == "github" else None
                for repository_id in scopes:
                    reference: dict[str, Any] = {
                        "provider": provider, "personal_owner": owner,
                        "repository_id": repository_id, "access": access, "policy_audit": audit,
                        "epoch": authority(credentials, row["encrypted"]),
                        "read_only": bool(policy["read_only"] or owner),
                    }
                    if provider == "github":
                        reference["app_version"] = app_version
                    digest = hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()
                    identity = str(uuid5(NAMESPACE_URL, "moyai/canonical-connection/" + digest))
                    result.append(Grant(
                        id=identity, kind=provider, revision=int(digest[:7], 16) + 1,
                        credential="", read_only=reference["read_only"],
                        policy={"require_approval": False, "product": {
                            "provider": provider, "personal_owner": owner, "access": access,
                            "repository_id": repository_id, "authority": digest,
                        }},
                    ))
        return result

    async def resolve_grants(
        self, actor_id: str, selections: list[str], *,
        shared_session: bool = False, surface: str = "web", db: AsyncSession | None = None,
    ) -> list[Grant]:
        if not selections:
            return []
        if db is not None:
            from types import SimpleNamespace

            from app.connectors import Connectors
            from app.requester_identity import same_requester

            from .product_connection_store import ConnectionSnapshot

            def snapshot(session):
                source = ConnectionSnapshot(session, self.product.identity.schema)
                connectors: Any = Connectors(cast(Any, source), self.security, self.settings)
                connectors.slack_identities = SimpleNamespace(
                    same_requester=lambda owner, actor: same_requester(source, owner, actor),
                )
                return self._grants(
                    actor_id, shared_session=shared_session, surface=surface,
                    source_store=source, source_connectors=connectors,
                )

            # run_sync adapts the canonical pure helpers to async SQL on this
            # exact connection; it never checks out a source Store pool slot.
            available = await db.run_sync(snapshot)
        else:
            available = await source_database(
                self._grants, actor_id, shared_session=shared_session, surface=surface,
            )
        selected: dict[str, Grant] = {}
        for selection in dict.fromkeys(selections):
            matches = [g for g in available if g.id == selection or g.kind == selection]
            if not matches:
                raise HTTPException(
                    403, "A selected product account changed or is unavailable on this surface. "
                    "Refresh Connections and select the current account.",
                )
            selected.update((g.id, g) for g in matches)
        return sorted(selected.values(), key=lambda item: item.id)

    async def slack_installation(self, *, db: AsyncSession | None = None) -> dict[str, Any]:
        if db is None:
            return await source_database(self.connectors.slack_installation)
        from app.connectors import Connectors

        from .product_connection_store import ConnectionSnapshot

        def snapshot(session):
            source = ConnectionSnapshot(session, self.product.identity.schema)
            connectors = Connectors(cast(Any, source), self.security, self.settings)
            return connectors.slack_installation()

        return await db.run_sync(snapshot)

    async def require_scope(self, scope: RunScope) -> None:
        current = await self.resolve_grants(
            scope.actor_id, [g.id for g in scope.grants],
            shared_session=scope.visibility == "shared", surface=scope.surface,
        )
        if [(g.id, g.policy) for g in current] != [
            (g.id, g.policy) for g in sorted(scope.grants, key=lambda item: item.id)
        ]:
            raise PermissionError("The selected product account authority changed.")

    async def check(self, scope: RunScope, grant: Grant) -> None:
        if grant not in scope.grants or not grant.policy.get("product"):
            raise PermissionError("This product account was not selected.")
        await self.database.authorize_scope(scope)
        await self.require_scope(scope)

    async def source_call(self, operation: Any) -> Any:
        """Run mixed async/sync canonical helpers away from the ASGI loop.

        Helpers retain their existing sync Store API. Each invocation owns its
        asyncio locks; cross-process refresh safety comes from SQL CAS, not those
        locks. database() drains cancellation before returning to its caller.
        """
        def execute():
            bound: Any = copy.copy(self.connectors)
            bound.locks = {provider: asyncio.Lock() for provider in PROVIDERS}
            bound.personal_slack = copy.copy(bound.personal_slack)
            bound.personal_slack.connectors, bound.personal_slack.locks = bound, {}
            bound.github = copy.copy(bound.github)
            bound.github.connectors = bound
            for key, value in vars(bound.github).items():
                if isinstance(value, asyncio.Lock):
                    setattr(bound.github, key, asyncio.Lock())
            return asyncio.run(operation(bound))

        return await source_database(execute)

    async def credentials(self, scope: RunScope, grant: Grant, *, write: bool = False) -> dict[str, Any]:
        await self.check(scope, grant)
        if write and grant.read_only:
            raise PermissionError("This product account is read only.")
        async with self.connectors.locks[grant.kind]:
            value = await self._credentials(grant, write=write)
        await self.check(scope, grant)
        return value

    async def _credentials(self, grant: Grant, *, write: bool) -> dict[str, Any]:
        reference = grant.policy["product"]
        if reference.get("access") == "bot":
            value = {"access_token": await self.source_call(lambda connectors: connectors.slack_bot_token())}
        elif reference["personal_owner"]:
            value, _ = await self.source_call(
                lambda connectors: connectors.personal_slack.credentials(reference["personal_owner"]),
            )
        elif grant.kind == "github":
            token = await self.source_call(
                lambda connectors: connectors.github.installation_token(
                    credentials=connectors.github.saved_credentials(),
                    repository=reference["repository_id"], write=write,
                ),
            )
            value = {"access_token": token}
        else:
            value = await self.source_call(lambda connectors: connectors.credentials(grant.kind))
        return value

    async def bot_token(self, scope: RunScope, grant: Grant) -> str:
        await self.check(scope, grant)
        async with self.connectors.locks["slack"]:
            token = await self.source_call(lambda connectors: connectors.slack_bot_token())
        await self.check(scope, grant)
        return token

    async def require_bot_dm(self, scope: RunScope, channel: str) -> None:
        if not channel.startswith("D"):
            return
        from sqlalchemy import select

        from .models import SlackThread
        from .product_slack_models import ProductSlackBinding

        async with self.database.sessions() as db:
            owner = await db.scalar(select(ProductSlackBinding.requester_id).join(
                SlackThread, SlackThread.session_id == ProductSlackBinding.session_id,
            ).where(
                ProductSlackBinding.session_id == scope.session_id, SlackThread.channel_id == channel,
            ))
        if owner is None or not (
            owner == scope.actor_id or await source_database(self.identities.same_requester, scope.actor_id, owner)
        ):
            raise ToolFailed(
                "Bot DM reads are limited to this Slack conversation's verified requester. "
                "Use personal Slack in a private web chat for other personal reads."
            )

    async def catalog(
        self, actor_id: str, *, shared_session: bool = False, surface: str = "web",
    ) -> list[dict[str, Any]]:
        metadata = {row["id"]: row for row in await self.accounts()}
        grants = await source_database(
            self._grants, actor_id, shared_session=shared_session, surface=surface,
        )
        result = []
        for grant in grants:
            ref = grant.policy["product"]
            account = metadata.get(grant.kind, {})
            name = str(account.get("label") or grant.kind.title())
            if ref["repository_id"]:
                name = next((
                    repository["full_name"] for repository in account.get("repositories", [])
                    if repository["id"] == ref["repository_id"]
                ), f"Repository {ref['repository_id']}")
            if ref["personal_owner"]:
                name = "Personal Slack"
            elif ref.get("access") == "bot":
                name = "Slack bot"
            result.append({
                "id": grant.id, "name": name, "kind": grant.kind,
                "owner_id": ref["personal_owner"] or "", "shared": not bool(ref["personal_owner"]),
                "read_only": grant.read_only, "enabled": True, "revision": grant.revision,
                "policy": grant.policy, "source": "product",
            })
        return result

    async def accounts(self) -> list[dict[str, Any]]:
        from app.connector_errors import ConnectorError

        saved = await source_database(self.connectors.github.saved_credentials)
        if saved and "repository_ids" not in saved:
            try:
                await self.source_call(lambda connectors: connectors.github.ensure_connection())
            except ConnectorError:
                await source_database(self.connectors.record_check, "github", "needs_attention")
        return await source_database(self.connectors.list)

    def toolset(self, scope: RunScope, grant: Grant) -> AbstractToolset[RunScope]:
        if grant.kind == "github":
            return ProductGitHubToolset(self, scope, grant)
        return self._canonical_tools(scope, grant)

    async def prepare_repository(self, scope: RunScope, sandbox: Any, context: dict[str, Any]) -> dict[str, Any]:
        from .product_github_bootstrap import prepare_repository
        return await prepare_repository(self, scope, sandbox, context)

    async def repository_context(self, scope: RunScope, context: dict[str, Any]) -> dict[str, Any] | None:
        from .product_github_bootstrap import repository_context
        return await repository_context(self, scope, context)

    async def repository_receipt(self, db, *, session_id: str, workspace_id: str, workspace_scope: str, repo_url: str) -> dict[str, Any] | None:
        from .product_github_bootstrap import repository_receipt
        return await repository_receipt(
            self, db, session_id=session_id, workspace_id=workspace_id, workspace_scope=workspace_scope, repo_url=repo_url,
        )

    async def resolve_repository(self, grant, repository: str = "", repository_id: int | None = None, *, selected: bool) -> dict[str, Any]:
        """Resolve a permanent ID outside a caller's writer transaction."""
        principal = await self.product.identity.require_current(grant)
        if selected:
            before = await self.resolve_grants(principal.id, ["github"], shared_session=True, surface="automation")
            target = repository_id if repository_id is not None else await source_database(
                self.connectors.github.target, repository,
            )
            original = next((item for item in before if item.policy["product"]["repository_id"] == target), None)
            if original is None:
                raise HTTPException(403, "This repository is not among the requester's selected GitHub grants.")
            main_loop = asyncio.get_running_loop()
            async def current_selection():
                await self.product.identity.require_current(grant)
                current = await self.resolve_grants(principal.id, [original.id], shared_session=True, surface="automation")
                if original not in current:
                    raise HTTPException(409, "The selected GitHub authority changed during repository lookup.")
            async def resolve(bound):
                source_request = bound.github.request
                async def guarded(*args, **kwargs):
                    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(current_selection(), main_loop))
                    return await source_request(*args, **kwargs)
                bound.github.request = guarded
                identity = await bound.github.selected_target({}, repository=repository, repository_id=target)
                return identity, bound.github.repository_name(identity)
            target, name = await self.source_call(resolve)
            await current_selection()
            result = {"repository_id": target, "repository": name, "repo_url": "https://github.com/" + name,
                      "connection_id": original.id, "public": False}
        else:
            value = await self.public_repository(repository, repository_id, refresh=True)
            result = {"repository_id": value["id"], "repository": value["full_name"],
                      "repo_url": "https://github.com/" + value["full_name"], "connection_id": None, "public": True}
        await self.product.identity.require_current(grant)
        return result

    async def public_repository(
        self, repository: str = "", repository_id: int | None = None, *, refresh: bool = False,
    ) -> dict[str, Any]:
        """Bound repeated anonymous metadata reads by immutable ID, never cache authority.

        New name resolution/admission and explicit refresh always revalidate.
        The cache contains only public metadata; failed refreshes evict it and
        never borrow an organization token or serve expired state.
        """
        if repository_id is None:
            value = await self.source_call(lambda bound: bound.github.public_repository(repository))
            self._cache_public_repository(value)
            return copy.deepcopy(value)
        entry = self._public_repositories.get(repository_id)
        if not refresh and entry and time.monotonic() - entry[0] < 300:
            return copy.deepcopy(entry[1])
        lock = self._public_repository_locks.setdefault(repository_id, asyncio.Lock())
        async with lock:
            entry = self._public_repositories.get(repository_id)
            if not refresh and entry and time.monotonic() - entry[0] < 300:
                return copy.deepcopy(entry[1])
            try:
                value = await self.source_call(lambda bound: bound.github.public_repository(repository, repository_id))
                if value.get("id") != repository_id:
                    raise HTTPException(403, "GitHub did not confirm the requested permanent repository ID.")
                self._cache_public_repository(value)
            except BaseException:
                self._public_repositories.pop(repository_id, None)
                raise
            return copy.deepcopy(value)

    def _cache_public_repository(self, value: dict[str, Any]) -> None:
        if type(value.get("id")) is not int or value["id"] <= 0 or value.get("private") is not False:
            raise HTTPException(403, "GitHub did not confirm a public repository.")
        self._public_repositories[value["id"]] = (time.monotonic(), copy.deepcopy(value))
        while len(self._public_repositories) > 64:
            oldest = min(self._public_repositories, key=lambda key: self._public_repositories[key][0])
            self._public_repositories.pop(oldest, None)

    def _canonical_tools(self, scope: RunScope, grant: Grant) -> AbstractToolset[RunScope]:
        """Use canonical REST operations for API keys as well as OAuth tokens.

        Hosted Notion/Slack MCP OAuth is not interchangeable with an existing
        integration token. Native FunctionToolset owns schema validation/execution;
        the canonical provider helpers own their fixed-origin REST protocol.
        """
        from app.connectors import TOOLS

        tools = []
        for name, (provider, write, schema, description) in TOOLS.items():
            if provider != grant.kind or (write and grant.read_only):
                continue
            # These old operations depend on canonical run-bound file/identity
            # services; the bot adapter exposes their explicit compatibility state.
            if provider == "slack" and name not in {"slack_search", "slack_channel", "slack_thread", "slack_me", "slack_send"}:
                continue
            if grant.policy["product"].get("access") == "bot" and name == "slack_search":
                continue

            def build(tool_name: str, model: Any, is_write: bool):
                async def invoke(**arguments: Any) -> Any:
                    from app.connector_errors import ConnectorError
                    from app.personal_slack import Selection

                    args = model.model_validate(arguments).model_dump()
                    if grant.policy["product"].get("access") == "bot" and tool_name in {"slack_channel", "slack_thread"}:
                        args["as_bot"] = True
                    bot_only = grant.kind == "slack" and (
                        tool_name in {"slack_me", "slack_send"} or args.get("as_bot") is True
                    )
                    if bot_only:
                        await self.check(scope, grant)
                        if grant.policy["product"]["personal_owner"]:
                            raise ToolFailed("Bot access requires the organization Slack account selection.")
                        if tool_name in {"slack_thread", "slack_channel"}:
                            await self.require_bot_dm(scope, args["channel"])
                        credentials = {}
                        bot_token = await self.bot_token(scope, grant)
                    else:
                        credentials = await self.credentials(scope, grant, write=is_write)
                        bot_token = None
                    main_loop = asyncio.get_running_loop()

                    async def check_current():
                        # Native SQL resources remain on their owning event loop.
                        future = asyncio.run_coroutine_threadsafe(self.check(scope, grant), main_loop)
                        return await asyncio.wrap_future(future)

                    async def operate(bound: Any):
                        source_request = bound.request

                        async def guarded_request(*request_args: Any, **kwargs: Any):
                            await check_current()
                            value = await source_request(*request_args, **kwargs)
                            if not is_write:
                                await check_current()
                            return value

                        async def selected_credentials(_provider: str):
                            await check_current()
                            return credentials

                        async def selected_slack(*_args: Any, **_kwargs: Any):
                            return Selection(grant.policy["product"]["personal_owner"], None, None, tool_name, credentials)

                        async def selected_bot_token():
                            await check_current()
                            if bot_token is None:
                                raise ToolFailed("The bot account was not selected for this operation.")
                            return bot_token

                        # Native current-scope checks execute before every provider
                        # await; no fabricated legacy run or source execution row.
                        bound.request = guarded_request
                        bound.credentials = selected_credentials
                        if bot_only:
                            bound.slack_bot_token = selected_bot_token
                        bound.allowed = lambda selected_name, **_kwargs: (
                            selected_name in TOOLS and TOOLS[selected_name][0] == grant.kind
                            and (not TOOLS[selected_name][1] or not grant.read_only)
                        )
                        bound.check_execution = lambda _run: None
                        bound.guard = lambda *_args, **_kwargs: None
                        bound.personal_slack = copy.copy(bound.personal_slack)
                        bound.personal_slack.select = selected_slack
                        bound.personal_slack.recheck = lambda _selection: None
                        bound.slack_identities = copy.copy(self.identities)
                        bound.slack_identities.connectors = bound
                        bound.slack_identities.check_requester = lambda *_args, **_kwargs: None
                        try:
                            if tool_name == "linear_my_issues":
                                return await bound.my_linear_issues({"active_user_id": scope.actor_id}, args)
                            if grant.kind == "slack" and args.get("as_bot"):
                                # Bot reads use the independently refreshed bot token.
                                await check_current()
                            result = await bound.call(
                                tool_name, args, run={"id": scope.session_id, "active_user_id": scope.actor_id}
                                if tool_name in {"slack_me", "slack_send"} else None,
                            )
                        except ConnectorError as error:
                            # Raising preserves EffectReceipts' uncertainty barrier.
                            # Provider text is safe; credential payloads are not returned.
                            raise ToolFailed(str(error)) from None
                        if is_write and isinstance(result, dict) and (
                            result.get("outcome_uncertain") or result.get("error") or result.get("complete") is False
                        ):
                            raise ToolFailed("The provider did not confirm the complete action. Verify before retrying.")
                        return result
                    return await self.source_call(operate)
                return invoke

            tool = Tool.from_schema(
                build(name, schema, write), name=name.removeprefix(provider + "_"),
                description=description, json_schema=schema.model_json_schema(),
            )
            tool.metadata = {"annotations": {"readOnlyHint": not write}}
            tools.append(tool)
        return FunctionToolset(tools, id=f"connection_{grant.id}", max_retries=0)


class CommittedSourceWrites:
    """Canonical PostgreSQL writes already commit before their helpers return."""

    async def flush(self) -> None:
        return None


class ProductGitHubToolset(AbstractToolset[RunScope]):
    """Scoped native reads plus canonical publication/approval operations."""

    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant
        from .product_github_publication import toolset
        self.publications = toolset(service, scope, grant)

    @property
    def id(self) -> str:
        return f"connection_{self.grant.id}"

    async def _native(self, *, write: bool = False):
        from pydantic_ai_harness.github import GitHub

        value = await self.service.credentials(self.scope, self.grant, write=write)
        return GitHub[RunScope](
            id=self.id, auth=value["access_token"], read_only=self.grant.read_only,
            include_instructions=False,
        ).get_toolset()

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        from .connections import PROHIBITED_TOOLS, read_only_tool

        native = await self._native()
        async with native:
            tools = await native.get_tools(ctx)
        # Unknown writes/global searches are never inherited from a catalog change.
        selected = {
            name: tool for name, tool in tools.items()
            if name not in PROHIBITED_TOOLS and read_only_tool(tool.tool_def)
            and ({"owner", "repo"} <= set(tool.tool_def.parameters_json_schema.get("properties", {}))
                 or name in {"search_code", "search_issues", "search_pull_requests"})
        }
        selected.update(await self.publications.get_tools(ctx))
        return {name: replace(tool, toolset=self) for name, tool in selected.items()}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        from .connections import read_only_tool

        own = await self.publications.get_tools(ctx)
        if name in own:
            return await self.publications.call_tool(name, tool_args, ctx, own[name])
        write = not read_only_tool(tool.tool_def)
        if write:
            raise ToolFailed("This GitHub write requires the canonical PR publication adapter.")
        repository_id = self.grant.policy["product"]["repository_id"]
        if name in {"search_code", "search_issues", "search_pull_requests"}:
            from app.github_coding import quoted_search_query
            repository = await source_database(self.service.connectors.github.repository_name, repository_id)
            try:
                query = quoted_search_query(tool_args.get("query", ""))
            except (ValueError, TypeError):
                raise ToolFailed("Use plain search terms or quoted phrases; repository scope is supplied by the server.") from None
            tool_args = {**tool_args, "query": query + " repo:" + repository}
        else:
            target = await source_database(
                self.service.connectors.github.target,
                f"{tool_args.get('owner', '')}/{tool_args.get('repo', '')}",
            )
            if target != repository_id:
                raise ToolFailed("This tool is scoped to a different selected repository.")
        native = await self._native(write=write)
        async with native:
            definitions = await native.get_tools(ctx)
            current = definitions.get(name)
            if current is None or read_only_tool(current.tool_def) != read_only_tool(tool.tool_def):
                raise ToolFailed("The provider tool definition changed. No action was sent.")
            await self.service.check(self.scope, self.grant)
            result = await native.call_tool(name, tool_args, ctx, current)
            if not write:
                await self.service.check(self.scope, self.grant)
            return result


class TokenConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=100_000)


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    read_only: bool


class ConnectionRoute(APIRoute):
    def get_route_handler(self):
        from app.connector_errors import ConnectorError

        handler = super().get_route_handler()

        async def safe(request: Request):
            try:
                return await handler(request)
            except ConnectorError as error:
                return JSONResponse({"detail": str(error)}, status_code=502)
            except RequestValidationError as error:
                return JSONResponse(status_code=422, content={"detail": [
                    {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
                ]})
        return safe


def routes(service: ProductConnections) -> APIRouter:
    """Mount before native CRUD/dynamic connection-ID routes in product mode."""
    from app.connection_authority import save_if_unchanged, set_policy
    from app.connector_errors import ConnectorError
    from app.github_setup import routes as github_routes
    from app.security import digest

    connectors, security, store = service.connectors, service.security, service.store

    async def authenticated(request: Request):
        await service.product.identity.authenticate(
            request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"},
        )

    router = APIRouter(dependencies=[Depends(authenticated)], route_class=ConnectionRoute)
    canonical_github = github_routes(connectors, security, store, service.settings)

    def offloaded_github(original: Any, path: str):
        @wraps(original)
        async def endpoint(**kwargs: Any):
            request = kwargs["request"]
            await source_database(
                security.require, request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"}, admin=True,
            )
            # These source routes use body()/JSON only, never streaming. Read
            # the ASGI body on its owning loop before entering source helpers.
            await request.body()

            async def invoke(bound: Any):
                source_router = github_routes(bound, security, store, service.settings)
                selected = next(
                    route.endpoint for route in source_router.routes
                    if isinstance(route, APIRoute) and route.path == path and request.method in (route.methods or set())
                )
                return await selected(**kwargs)

            # Preserve the source service's process-local setup serialization;
            # database CAS/source authority remain the cross-process fences.
            async with connectors.github.setup_lock:
                return await service.source_call(invoke)
        return endpoint

    for route in canonical_github.routes:
        if isinstance(route, APIRoute):
            router.add_api_route(
                route.path, offloaded_github(route.endpoint, route.path), methods=list(route.methods or {"GET"}),
                name=route.name, status_code=route.status_code,
            )
    personal_callback = connectors.personal_slack.routes(router)

    @router.get("/api/product/connections")
    async def accounts(request: Request):
        await source_database(security.require, request)
        return await service.accounts()

    @router.post("/api/connections/{provider}")
    async def connect(provider: Provider, body: TokenConnection, request: Request):
        await source_database(security.require, request, mutation=True, admin=True)
        if provider == "github":
            raise HTTPException(400, "Install the organization GitHub App; raw tokens are not accepted.")
        value = {"access_token": body.token.get_secret_value().strip(), "kind": "personal"}
        expected = await source_database(connectors.connection_revision, provider)
        label = await connectors.verify(provider, value)
        await service.product.identity.require_request_current(request)
        await source_database(save_if_unchanged, store, security, provider, value, label, expected)
        await source_database(connectors.record_check, provider, "healthy")
        await source_database(connectors.audit, provider, "Connected for the organization")
        return {"connected": True, "label": label}

    @router.delete("/api/connections/{provider}")
    async def disconnect(provider: Provider, request: Request):
        await source_database(security.require, request, mutation=True, admin=True)
        async with connectors.locks[provider]:
            def revoke():
                with store.connect() as conn:
                    conn.begin_write()
                    conn.execute("DELETE FROM connections WHERE provider=?", (provider,))
                    conn.execute("DELETE FROM oauth_states WHERE provider=?", (provider,))
            await source_database(revoke)
        await source_database(connectors.expire_approvals, provider)
        await source_database(connectors.audit, provider, "Disconnected from the organization")
        return {"connected": False}

    @router.patch("/api/connections/{provider}/policy")
    async def policy(provider: Provider, body: PolicyChange, request: Request):
        await source_database(security.require, request, mutation=True, admin=True)
        await source_database(set_policy, store, security, provider, body.enabled, body.read_only)
        if not body.enabled or body.read_only:
            await source_database(connectors.expire_approvals, provider)
        await source_database(connectors.audit, provider, "Updated organization connection policy")
        return await source_database(connectors.policy, provider)

    @router.post("/api/connections/{provider}/check")
    async def check(provider: Provider, request: Request):
        await source_database(security.require, request, mutation=True, admin=True)
        try:
            if provider == "github":
                await service.source_call(lambda bound: bound.github.refresh_connection())
                label = ", ".join(await source_database(connectors.github.targets))
            else:
                async def verify(bound: Any):
                    return await bound.verify(provider, await bound.credentials(provider))
                label = await service.source_call(verify)
        except ConnectorError:
            await source_database(connectors.record_check, provider, "needs_attention")
            raise
        await source_database(connectors.record_check, provider, "healthy")
        return {"healthy": True, "label": label}

    @router.post("/api/connections/{provider}/oauth")
    async def oauth(provider: Provider, request: Request):
        sid = await source_database(security.require, request, mutation=True, admin=True)
        if not connectors.configured_oauth(provider):
            raise HTTPException(409, "Configure this app's OAuth client or connect using a token.")
        state = secrets.token_urlsafe(32)
        await source_database(store.execute, "DELETE FROM oauth_states WHERE expires<?", (time.time(),))
        await source_database(
            store.execute, "INSERT INTO oauth_states VALUES(?,?,?,?)",
            (digest(state), provider, digest(sid), time.time() + 600),
        )
        return {"url": connectors.authorization_url(provider, state)}

    @router.get("/oauth/{provider}/callback")
    async def callback(provider: Provider, request: Request, state: str = "", code: str = "", error: str = ""):
        if provider == "slack" and connectors.personal_slack.is_oauth_state(state):
            return await personal_callback(request, state=state, code=code, error=error)
        sid = await source_database(security.require, request, admin=True)

        def consume():
            with store.connect() as conn:
                return conn.execute(
                    """DELETE FROM oauth_states WHERE state_hash=? AND provider=?
                       AND session_id=? AND expires>? RETURNING state_hash""",
                    (digest(state), provider, digest(sid), time.time()),
                ).fetchone()

        if not await source_database(consume):
            raise HTTPException(400, "Expired or invalid connection request. Start again from Connections.")
        if error or not code:
            return RedirectResponse("/?connection=cancelled#connections", status_code=303)
        expected = await source_database(connectors.connection_revision, provider)
        value = await connectors.exchange(provider, code=code)
        label = await connectors.verify(provider, value)
        await service.product.identity.require_request_current(request)
        await source_database(save_if_unchanged, store, security, provider, value, label, expected)
        await source_database(connectors.record_check, provider, "healthy")
        await source_database(connectors.audit, provider, "Authorized an organization connection")
        return RedirectResponse("/?connection=success#connections", status_code=303)

    return router
