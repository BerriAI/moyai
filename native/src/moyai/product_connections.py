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
import ipaddress
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakValueDictionary

from app.db import database as source_database
from app.public_url import app_path
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_ai import RunContext, Tool
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from .connection_catalog import CatalogToolset, ConnectionCatalog, ConnectionDefinitionChanged
from .contracts import Grant, RunScope
from .product_connector_execution import CanonicalConnectorExecution

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .product_resources import ProductResources

Provider = Literal["github", "gitlab", "linear", "notion", "slack"]
PROVIDERS = ("github", "gitlab", "linear", "notion", "slack")


def _gitlab_origin(value: str) -> str:
    """HTTPS origin; explicit literal loopback HTTP supports local installations."""
    from app.repository_identity import canonical_origin

    try:
        normalized = canonical_origin(value)
        parts = urlsplit(normalized)
        if parts.scheme == "http" and not ipaddress.ip_address(parts.hostname or "").is_loopback:
            raise ValueError
        return normalized
    except (ValueError, TypeError):
        raise ValueError(
            "Use an HTTPS GitLab origin without credentials, an API path, query, or fragment. "
            "HTTP is allowed only for a literal loopback address."
        ) from None


def _gitlab_revision(encrypted: str | None) -> str:
    # Opaque compare-and-set version; never return ciphertext to the browser.
    return hashlib.sha256((encrypted or "").encode()).hexdigest()


class ProductConnections:
    def __init__(self, product: ProductResources, database: Database):
        from app.connectors import Connectors
        from app.identities import SlackIdentities
        from app.requester_identity import same_requester

        self.product, self.database = product, database
        self.store, self.security, self.settings = product.store_proxy, product.security, product.settings
        self.connectors = Connectors(cast(Any, self.store), self.security, self.settings)
        self.github_mcp_url: str | None = None
        self.identities = SlackIdentities(
            self.store, self.connectors, self.settings, self.security, CommittedSourceWrites(),
            lambda owner, actor: same_requester(self.store, owner, actor),
        )
        cast(Any, self.connectors).slack_identities = self.identities
        self.execution = CanonicalConnectorExecution(self)
        self._github_credential_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
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
        source_store: Any = None, source_connectors: Any = None, selected_ids: Sequence[str] = (),
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
            # Slack conversations use the installed organization account. A
            # participant's private-web setup must not change shared authority.
            owner = personal.owner(actor_id) if provider == "slack" and surface != "slack" else None
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
                if provider == "slack" and surface == "slack" and selected_ids:
                    organization = json.loads(self.security.decrypt(rows[0]["encrypted"]))
                    if (organization.get("bot") or {}).get("access_token"):
                        # Existing turns/workspaces may retain the former
                        # bot-only grant. Resolve its exact ID without adding
                        # it to the catalog or broadening its permissions.
                        selected.append((rows[0], None, "bot"))
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
                origin = None
                if provider == "gitlab":
                    try:
                        origin = _gitlab_origin(credentials["base_url"])
                        ids = credentials["repository_ids"]
                        if not isinstance(ids, list) or not ids or any(type(item) is not int or item <= 0 for item in ids):
                            continue
                        scopes = sorted(set(ids))
                    except (KeyError, TypeError, ValueError):
                        # Incomplete or legacy token rows grant no project access.
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
                    if provider == "gitlab":
                        reference["base_url"] = origin
                    digest = hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()
                    identity = str(uuid5(NAMESPACE_URL, "moyai/canonical-connection/" + digest))
                    if surface == "slack" and access == "bot" and identity not in selected_ids:
                        continue
                    result.append(Grant(
                        id=identity, kind=provider, revision=int(digest[:7], 16) + 1,
                        credential="", read_only=reference["read_only"],
                        policy={"require_approval": False, "product": {
                            "provider": provider, "personal_owner": owner, "access": access,
                            "repository_id": repository_id, "authority": digest,
                            **({"base_url": origin} if provider == "gitlab" else {}),
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
                    source_store=source, source_connectors=connectors, selected_ids=selections,
                )

            # run_sync adapts the canonical pure helpers to async SQL on this
            # exact connection; it never checks out a source Store pool slot.
            available = await db.run_sync(snapshot)
        else:
            available = await source_database(
                self._grants, actor_id, shared_session=shared_session, surface=surface,
                selected_ids=selections,
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
            shared_session=scope.authority.visibility == "shared", surface=scope.authority.surface,
        )
        self._check_grants(scope, current)

    @staticmethod
    def _check_grants(scope: RunScope, current: list[Grant]) -> None:
        if [(g.id, g.policy) for g in current] != [
            (g.id, g.policy) for g in sorted(scope.grants, key=lambda item: item.id)
        ]:
            raise PermissionError("The selected product account authority changed.")

    async def check(self, scope: RunScope, grant: Grant) -> None:
        if grant not in scope.grants or not grant.policy.get("product"):
            raise PermissionError("This product account was not selected.")
        # Native authorization already resolves live canonical grants under the
        # persisted execution context. Compare full policy against that same
        # snapshot instead of opening another source connection to read it again.
        current = await self.database.authorize_scope(scope)
        self._check_grants(scope, current)

    async def credentials(self, scope: RunScope, grant: Grant, *, write: bool = False) -> dict[str, Any]:
        await self.check(scope, grant)
        if write and grant.read_only:
            raise PermissionError("This product account is read only.")
        async with self._credential_lock(grant):
            value = await self._credentials(grant, write=write)
        await self.check(scope, grant)
        return value

    async def read_credentials(
        self, grant: Grant, authorize: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        """Issue read credentials under a caller's live viewer/selection authority.

        Historical views have no running turn. Their authorizer must verify the
        current actor, session access, and exact selected grant before and after
        each await; execution credentials retain their separate running-scope check.
        """
        if not grant.policy.get("product"):
            raise PermissionError("This product account was not selected.")
        await authorize()
        async with self._credential_lock(grant):
            await authorize()
            value = await self._credentials(grant, write=False)
            await authorize()
        return value

    def _credential_lock(self, grant: Grant) -> asyncio.Lock:
        lock = self.connectors.locks[grant.kind]
        if grant.kind == "github":
            # Installation tokens are independently minted/cached per repository.
            # Retain single-flight for the same repository without serializing
            # every selected repository behind one provider-wide OAuth lock.
            # Other providers still serialize refresh of their shared credentials.
            repository = grant.policy["product"]["repository_id"]
            lock = self._github_credential_locks.setdefault(repository, asyncio.Lock())
        return lock

    async def _credentials(self, grant: Grant, *, write: bool) -> dict[str, Any]:
        reference = grant.policy["product"]
        if reference.get("access") == "bot":
            value = {"access_token": await self.execution.call(lambda connectors: connectors.slack_bot_token())}
        elif reference["personal_owner"]:
            value, _ = await self.execution.call(
                lambda connectors: connectors.personal_slack.credentials(reference["personal_owner"]),
            )
        elif grant.kind == "github":
            token = await self.execution.call(
                lambda connectors: connectors.github.installation_token(
                    credentials=connectors.github.saved_credentials(),
                    repository=reference["repository_id"], write=write,
                ),
            )
            value = {"access_token": token}
        elif grant.kind == "gitlab":
            credentials = await self.execution.call(lambda connectors: connectors.credentials("gitlab"))
            repository_id = reference["repository_id"]
            if (
                _gitlab_origin(credentials["base_url"]) != reference["base_url"]
                or repository_id not in credentials["repository_ids"]
            ):
                raise PermissionError("The selected GitLab project authority changed.")
            value = {
                "kind": "personal", "access_token": credentials["access_token"],
                "base_url": reference["base_url"], "repository_ids": [repository_id],
                "repositories": [row for row in credentials.get("repositories", []) if row["id"] == repository_id],
            }
        else:
            value = await self.execution.call(lambda connectors: connectors.credentials(grant.kind))
        return value

    async def bot_token(self, scope: RunScope, grant: Grant) -> str:
        await self.check(scope, grant)
        async with self.connectors.locks["slack"]:
            token = await self.execution.call(lambda connectors: connectors.slack_bot_token())
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
                await self.execution.call(lambda connectors: connectors.github.ensure_connection())
            except ConnectorError:
                await source_database(self.connectors.record_check, "github", "needs_attention")
        return await source_database(self.connectors.list)

    def toolset(self, scope: RunScope, grant: Grant) -> AbstractToolset[RunScope]:
        if grant.kind == "github":
            return ProductGitHubToolset(self, scope, grant)
        if grant.kind == "gitlab":
            from .product_gitlab import ProductGitLabToolset
            return ProductGitLabToolset(self, scope, grant)
        return self._canonical_tools(scope, grant)

    async def prepare_repository(self, scope: RunScope, sandbox: Any, context: dict[str, Any]) -> dict[str, Any]:
        from app.repository_identity import repository_provider

        if context.get("repository_provider") == "gitlab" or repository_provider(context.get("repo_url") or "") == "gitlab":
            from .product_gitlab_bootstrap import prepare_repository as prepare_gitlab
            return await prepare_gitlab(self, scope, sandbox, context)
        from .product_github_bootstrap import prepare_repository
        return await prepare_repository(self, scope, sandbox, context)

    async def repository_context(self, scope: RunScope, context: dict[str, Any]) -> dict[str, Any] | None:
        from app.repository_identity import repository_provider

        if context.get("repository_provider") == "gitlab" or repository_provider(context.get("repo_url") or "") == "gitlab":
            from .product_gitlab_bootstrap import repository_context as gitlab_context
            return await gitlab_context(self, scope, context)
        from .product_github_bootstrap import repository_context
        return await repository_context(self, scope, context)

    async def repository_receipt(self, db, *, session_id: str, workspace_id: str, workspace_scope: str, repo_url: str) -> dict[str, Any] | None:
        from app.repository_identity import repository_provider

        if repository_provider(repo_url) == "gitlab":
            from .product_gitlab_bootstrap import repository_receipt as gitlab_receipt
            return await gitlab_receipt(
                self, db, session_id=session_id, workspace_id=workspace_id, workspace_scope=workspace_scope, repo_url=repo_url,
            )
        from .product_github_bootstrap import repository_receipt
        return await repository_receipt(
            self, db, session_id=session_id, workspace_id=workspace_id, workspace_scope=workspace_scope, repo_url=repo_url,
        )

    async def resolve_repository(
        self, grant, repository: str = "", repository_id: int | None = None, *,
        selected: bool, provider: Literal["github", "gitlab"] = "github", base_url: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a permanent ID outside a caller's writer transaction."""
        if provider == "gitlab":
            from .product_gitlab_bootstrap import resolve_repository as resolve_gitlab
            return await resolve_gitlab(self, grant, repository, repository_id, selected=selected, base_url=base_url)
        principal = await self.product.identity.require_current(grant)
        if selected:
            before = await self.resolve_grants(principal.id, ["github"], shared_session=True, surface="automation")
            target = repository_id if repository_id is not None else await source_database(
                self.connectors.github.target, repository,
            )
            original = next((item for item in before if item.policy["product"]["repository_id"] == target), None)
            if original is None:
                raise HTTPException(403, "This repository is not among the requester's selected GitHub grants.")
            async def current_selection():
                await self.product.identity.require_current(grant)
                current = await self.resolve_grants(principal.id, [original.id], shared_session=True, surface="automation")
                if original not in current:
                    raise HTTPException(409, "The selected GitHub authority changed during repository lookup.")
            async def resolve(bound):
                identity = await bound.github.selected_target({}, repository=repository, repository_id=target)
                return identity, bound.github.repository_name(identity)
            target, name = await self.execution.call(resolve, authorize=current_selection)
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
            value = await self.execution.call(lambda bound: bound.github.public_repository(repository))
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
                value = await self.execution.call(lambda bound: bound.github.public_repository(repository, repository_id))
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

            def build(tool_name: str):
                async def invoke(**arguments: Any) -> Any:
                    return await self.execution.tool(scope, grant, tool_name, arguments)
                return invoke

            tool = Tool.from_schema(
                build(name), name=name.removeprefix(provider + "_"),
                description=description, json_schema=schema.model_json_schema(),
            )
            tool.metadata = {"annotations": {"readOnlyHint": not write}}
            tools.append(tool)
        return FunctionToolset(tools, id=f"connection_{grant.id}", max_retries=0)


class CommittedSourceWrites:
    """Canonical PostgreSQL writes already commit before their helpers return."""

    async def flush(self) -> None:
        return None


class ProductCatalogToolset(CatalogToolset):
    """Reuse canonical definitions across turns, never a turn's execution state.

    Only Moyai's canonical adapters opt in. Their schemas depend on the selected
    grants (including read/write policy), not the turn ID: REST tools come from
    fixed schemas; GitHub reads come from the admitted account's MCP catalog;
    publication definitions are fixed even though their closures enforce the
    current turn's approval/effect receipts. Those closures are never cached.

    Keep session, actor, product identity, provenance and the complete grant
    snapshot in the key. Arbitrary provider/context-dependent toolsets continue
    to use CatalogToolset's full-turn key.
    """

    def __init__(
        self, catalog: ConnectionCatalog, ctx: RunContext[RunScope],
        factory: Callable[[str | None], AbstractToolset[RunScope]],
        *, reconstruct: Callable[[ToolDefinition], AbstractToolset[RunScope] | None] | None = None,
    ):
        super().__init__(catalog, ctx, factory, reconstruct=reconstruct)
        self.key = catalog.key(replace(ctx, deps=replace(ctx.deps, turn_id="")))

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        from .runtime import current

        service = current().product_connections
        if service is None or not ctx.deps.grants or any(
            not grant.policy.get("product") for grant in ctx.deps.grants
        ):
            raise PermissionError("Canonical product connections are required for schema reuse.")
        try:
            tools = await super().get_tools(ctx)
            # A cache entry is not proof that this turn is still running or that
            # its requester/account is still authorized. Check the full current
            # scope at the release boundary, including on hits and after awaits.
            await service.check(ctx.deps, ctx.deps.grants[0])
            return tools
        except (PermissionError, HTTPException):
            self.catalog.discard(self.key)
            raise


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
        github = GitHub[RunScope](
            id=self.id, auth=value["access_token"], read_only=self.grant.read_only,
            include_instructions=False,
        )
        if self.service.github_mcp_url is not None:
            github.url = self.service.github_mcp_url
        return github.get_toolset()

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        from .connection_policy import PROHIBITED_TOOLS, read_only_tool

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
        repository = await source_database(
            self.service.connectors.github.repository_name, self.grant.policy["product"]["repository_id"],
        )
        return {
            name: replace(tool, toolset=self, tool_def=replace(
                tool.tool_def,
                description=(
                    f"Connected GitHub repository: {repository}. {tool.tool_def.description or ''}"
                    + (" Supply plain search terms; repository scope is added automatically."
                       if name in {"search_code", "search_issues", "search_pull_requests"} else "")
                ),
            ))
            for name, tool in selected.items()
        }

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope],
    ) -> Any:
        from .connection_policy import read_only_tool

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
            if current is None or (
                read_only_tool(current.tool_def) != read_only_tool(tool.tool_def)
                or current.tool_def.parameters_json_schema != tool.tool_def.parameters_json_schema
            ):
                raise ConnectionDefinitionChanged("The provider tool definition changed. Discover its tools again; no action was sent.")
            await self.service.check(self.scope, self.grant)
            arguments = current.args_validator.validate_python(tool_args)
            result = await native.call_tool(name, arguments, ctx, current)
            if not write:
                await self.service.check(self.scope, self.grant)
            return result


class TokenConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=100_000)


class GitLabTokenConnection(TokenConnection):
    token: SecretStr = Field(min_length=1, max_length=4096)
    base_url: str = Field(default="https://gitlab.com", max_length=2048)

    @field_validator("base_url")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        return _gitlab_origin(value)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Enter a GitLab access token.")
        return SecretStr(value.get_secret_value().strip())


class GitLabProjectDiscovery(GitLabTokenConnection):
    page: int = Field(default=1, ge=1, le=10_000)
    search: str = Field(default="", max_length=200)
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class GitLabProjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1, max_length=200)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class GitLabOrganizationSetup(GitLabTokenConnection, GitLabProjectSelection):
    pass


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    read_only: bool


class OrganizationConnectionResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class OrganizationConnectionTool(OrganizationConnectionResponse):
    name: str
    write: bool
    requires_approval: bool
    description: str


class OrganizationConnectionRepository(OrganizationConnectionResponse):
    id: int
    full_name: str


class GitLabProjectDiscoveryResult(OrganizationConnectionResponse):
    base_url: str
    repositories: list[OrganizationConnectionRepository]
    selected_ids: list[int]
    next_page: int | None
    truncated: bool
    revision: str


class OrganizationConnectionPolicy(OrganizationConnectionResponse):
    enabled: bool
    read_only: bool
    checked_at: str | None
    check_status: str | None


class OrganizationConnection(OrganizationConnectionPolicy):
    """One organization account; credentials stay on the server."""

    id: Provider
    connected: bool
    scope: Literal["organization"]
    oauth_configured: bool
    label: str
    updated_at: str | None
    identity: str
    tools: list[OrganizationConnectionTool]
    # Repository providers expose selected projects, never credentials.
    repositories: list[OrganizationConnectionRepository] | None = None
    app_registered: bool | None = None
    base_url: str | None = None


class OrganizationConnectionConnected(OrganizationConnectionResponse):
    connected: bool
    label: str


class OrganizationConnectionDisconnected(OrganizationConnectionResponse):
    connected: bool


class OrganizationConnectionCheck(OrganizationConnectionResponse):
    healthy: bool
    label: str


class OrganizationConnectionOAuthStart(OrganizationConnectionResponse):
    url: str


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
                return await service.execution.call(invoke)
        return endpoint

    for route in canonical_github.routes:
        if isinstance(route, APIRoute):
            router.add_api_route(
                route.path, offloaded_github(route.endpoint, route.path), methods=list(route.methods or {"GET"}),
                name=route.name, status_code=route.status_code,
                response_model=route.response_model, operation_id=route.operation_id,
                openapi_extra=route.openapi_extra, include_in_schema=route.include_in_schema,
            )
    personal_callback = connectors.personal_slack.routes(router)

    @router.get("/api/product/connections", operation_id="listOrganizationConnections")
    async def accounts(request: Request) -> list[OrganizationConnection]:
        await source_database(security.require, request)
        return [OrganizationConnection.model_validate(row) for row in await service.accounts()]

    async def gitlab_current(request: Request, expected: str | None) -> None:
        await service.product.identity.require_request_current(request)
        if await source_database(connectors.connection_revision, "gitlab") != expected:
            raise HTTPException(409, "The GitLab connection changed. Reload its projects before saving.")

    async def gitlab_projects(
        request: Request, credentials: dict[str, Any], expected: str | None, *, page: int, search: str,
    ) -> GitLabProjectDiscoveryResult:
        origin = _gitlab_origin(credentials["base_url"])

        async def discover(bound: Any):
            return await bound.gitlab.request(
                "GET", "/projects", token=credentials["access_token"], base_url=origin,
                params={"membership": True, "simple": True, "per_page": 100, "page": page,
                        "order_by": "id", "sort": "asc", "search": search},
            )

        values = await service.execution.call(
            discover, authorize=lambda: gitlab_current(request, expected), recheck=True,
        )
        if not isinstance(values, list) or len(values) > 100:
            raise ConnectorError("GitLab did not return a valid project page.", dispatched=False)
        repositories = [gitlab_project(value) for value in values]
        return GitLabProjectDiscoveryResult(
            base_url=origin, repositories=repositories,
            selected_ids=credentials.get("repository_ids", []),
            next_page=page + 1 if len(repositories) == 100 and page < 10_000 else None,
            truncated=len(repositories) == 100 and page == 10_000,
            revision=_gitlab_revision(expected),
        )

    def gitlab_project(value: Any, *, expected_id: int | None = None) -> OrganizationConnectionRepository:
        if (
            not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] <= 0
            or not isinstance(value.get("path_with_namespace"), str) or not value["path_with_namespace"]
            or (expected_id is not None and value["id"] != expected_id)
        ):
            raise ConnectorError("GitLab did not confirm the selected project identity.", dispatched=False)
        return OrganizationConnectionRepository(id=value["id"], full_name=value["path_with_namespace"])

    @router.post("/api/connections/gitlab/projects/discover", operation_id="discoverGitLabProjects")
    async def discover_gitlab(body: GitLabProjectDiscovery, request: Request) -> GitLabProjectDiscoveryResult:
        await source_database(security.require, request, mutation=True, admin=True)
        expected = await source_database(connectors.connection_revision, "gitlab")
        if body.revision is not None and body.revision != _gitlab_revision(expected):
            raise HTTPException(409, "The GitLab connection changed. Discover its projects again.")
        return await gitlab_projects(request, {
            "access_token": body.token.get_secret_value(), "base_url": body.base_url,
        }, expected, page=body.page, search=body.search)

    @router.get("/api/connections/gitlab/projects", operation_id="listGitLabProjects")
    async def list_gitlab(
        request: Request, page: int = Query(default=1, ge=1, le=10_000),
        search: str = Query(default="", max_length=200),
        revision: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
    ) -> GitLabProjectDiscoveryResult:
        await source_database(security.require, request, admin=True)
        expected = await source_database(connectors.connection_revision, "gitlab")
        if expected is None:
            raise HTTPException(409, "Connect GitLab before selecting projects.")
        if revision is not None and revision != _gitlab_revision(expected):
            raise HTTPException(409, "The GitLab connection changed. Reload its projects before saving.")
        credentials = json.loads(security.decrypt(expected))
        return await gitlab_projects(request, credentials, expected, page=page, search=search)

    async def save_gitlab(
        request: Request, body: GitLabProjectSelection, credentials: dict[str, Any], expected: str | None,
    ) -> OrganizationConnectionConnected:
        if body.revision != _gitlab_revision(expected):
            raise HTTPException(409, "The GitLab connection changed. Reload its projects before saving.")
        origin = _gitlab_origin(credentials["base_url"])

        async def verify(bound: Any):
            repositories = []
            for identity in sorted(set(body.repository_ids)):
                value = await bound.gitlab.request(
                    "GET", f"/projects/{identity}", token=credentials["access_token"], base_url=origin,
                )
                repositories.append(gitlab_project(value, expected_id=identity).model_dump())
            value = {
                "kind": "personal", "access_token": credentials["access_token"], "base_url": origin,
                "repository_ids": [row["id"] for row in repositories], "repositories": repositories,
            }
            label = await bound.gitlab.verify(value)
            return value, label

        credentials, label = await service.execution.call(
            verify, authorize=lambda: gitlab_current(request, expected), recheck=True,
        )
        await gitlab_current(request, expected)
        await source_database(save_if_unchanged, store, security, "gitlab", credentials, label, expected)
        await source_database(connectors.record_check, "gitlab", "healthy")
        await source_database(connectors.audit, "gitlab", "Updated organization GitLab projects")
        return OrganizationConnectionConnected(connected=True, label=label)

    @router.post("/api/connections/gitlab/connect", operation_id="connectGitLabOrganization")
    async def connect_gitlab(body: GitLabOrganizationSetup, request: Request) -> OrganizationConnectionConnected:
        await source_database(security.require, request, mutation=True, admin=True)
        expected = await source_database(connectors.connection_revision, "gitlab")
        return await save_gitlab(request, body, {
            "access_token": body.token.get_secret_value(), "base_url": body.base_url,
        }, expected)

    @router.put("/api/connections/gitlab/projects", operation_id="selectGitLabProjects")
    async def select_gitlab(body: GitLabProjectSelection, request: Request) -> OrganizationConnectionConnected:
        await source_database(security.require, request, mutation=True, admin=True)
        expected = await source_database(connectors.connection_revision, "gitlab")
        if expected is None:
            raise HTTPException(409, "Connect GitLab before selecting projects.")
        return await save_gitlab(request, body, json.loads(security.decrypt(expected)), expected)

    @router.post("/api/connections/{provider}", operation_id="connectOrganizationConnection")
    async def connect(provider: Provider, body: TokenConnection, request: Request) -> OrganizationConnectionConnected:
        await source_database(security.require, request, mutation=True, admin=True)
        if provider == "github":
            raise HTTPException(400, "Install the organization GitHub App; raw tokens are not accepted.")
        if provider == "gitlab":
            raise HTTPException(400, "Connect GitLab with an HTTPS origin and explicitly selected projects.")
        value = {"access_token": body.token.get_secret_value().strip(), "kind": "personal"}
        expected = await source_database(connectors.connection_revision, provider)
        label = await connectors.verify(provider, value)
        await service.product.identity.require_request_current(request)
        await source_database(save_if_unchanged, store, security, provider, value, label, expected)
        await source_database(connectors.record_check, provider, "healthy")
        await source_database(connectors.audit, provider, "Connected for the organization")
        return OrganizationConnectionConnected(connected=True, label=label)

    @router.delete("/api/connections/{provider}", operation_id="disconnectOrganizationConnection")
    async def disconnect(provider: Provider, request: Request) -> OrganizationConnectionDisconnected:
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
        return OrganizationConnectionDisconnected(connected=False)

    @router.patch("/api/connections/{provider}/policy", operation_id="updateOrganizationConnectionPolicy")
    async def policy(provider: Provider, body: PolicyChange, request: Request) -> OrganizationConnectionPolicy:
        await source_database(security.require, request, mutation=True, admin=True)
        await source_database(set_policy, store, security, provider, body.enabled, body.read_only)
        if not body.enabled or body.read_only:
            await source_database(connectors.expire_approvals, provider)
        await source_database(connectors.audit, provider, "Updated organization connection policy")
        return OrganizationConnectionPolicy.model_validate(await source_database(connectors.policy, provider))

    @router.post("/api/connections/{provider}/check", operation_id="checkOrganizationConnection")
    async def check(provider: Provider, request: Request) -> OrganizationConnectionCheck:
        await source_database(security.require, request, mutation=True, admin=True)
        expected = await source_database(connectors.connection_revision, provider)
        try:
            if provider == "github":
                await service.execution.call(lambda bound: bound.github.refresh_connection())
                label = ", ".join(await source_database(connectors.github.targets))
            else:
                async def verify(bound: Any):
                    return await bound.verify(provider, await bound.credentials(provider))
                label = await service.execution.call(
                    verify,
                    authorize=(lambda: gitlab_current(request, expected)) if provider == "gitlab" else None,
                    recheck=provider == "gitlab",
                )
        except ConnectorError:
            await source_database(connectors.record_check, provider, "needs_attention")
            raise
        await source_database(connectors.record_check, provider, "healthy")
        return OrganizationConnectionCheck(healthy=True, label=label)

    @router.post("/api/connections/{provider}/oauth", operation_id="startOrganizationConnectionOAuth")
    async def oauth(provider: Provider, request: Request) -> OrganizationConnectionOAuthStart:
        sid = await source_database(security.require, request, mutation=True, admin=True)
        if not connectors.configured_oauth(provider):
            raise HTTPException(409, "Configure this app's OAuth client or connect using a token.")
        state = secrets.token_urlsafe(32)
        await source_database(store.execute, "DELETE FROM oauth_states WHERE expires<?", (time.time(),))
        await source_database(
            store.execute, "INSERT INTO oauth_states VALUES(?,?,?,?)",
            (digest(state), provider, digest(sid), time.time() + 600),
        )
        return OrganizationConnectionOAuthStart(url=connectors.authorization_url(provider, state))

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
            return RedirectResponse(app_path(service.product.settings.public_url, "/?connection=cancelled#connections"), status_code=303)
        expected = await source_database(connectors.connection_revision, provider)
        value = await connectors.exchange(provider, code=code)
        label = await connectors.verify(provider, value)
        await service.product.identity.require_request_current(request)
        await source_database(save_if_unchanged, store, security, provider, value, label, expected)
        await source_database(connectors.record_check, provider, "healthy")
        await source_database(connectors.audit, provider, "Authorized an organization connection")
        return RedirectResponse(app_path(service.product.settings.public_url, "/?connection=success#connections"), status_code=303)

    return router
