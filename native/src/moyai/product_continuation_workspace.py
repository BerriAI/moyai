"""Canonical project selection and verified file import for native workspaces.

Selection reads run inside the caller's source-fenced admission transaction.
Provider I/O happens later, before the native model's first request. Per-user
legacy images are never attached as native workspaces.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Grant, PreparedTurn, RunScope
from .models import Session, Turn
from .product_continuation_models import ProductContinuation, ProductExecutionContext
from .product_continuation_schema import identifier
from .product_identity import ProductGrant, ProductPrincipal

if TYPE_CHECKING:
    from .runtime import Runtime

REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
BUILD_ID = re.compile(r"^[0-9a-f]{32}$")
BUILD_COLUMNS = "id,environment_id,revision,recipe,resolved_recipe,snapshot_id,commit_sha,phase,sandbox_provider"


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def build_fingerprint(build: Any) -> str:
    # A completed build's sandbox ID/log can be cleaned after publication.
    # Those housekeeping writes must not invalidate its immutable image/recipe.
    return fingerprint({key: build.get(key) for key in (
        "id", "environment_id", "revision", "recipe", "resolved_recipe",
        "snapshot_id", "commit_sha", "sandbox_provider",
    )})


def canonical_repository(value: str) -> str:
    if not value:
        return ""
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise HTTPException(422, "Choose a canonical GitHub repository URL.")
    try:
        parsed = urlsplit(value)
        authority = value.split("://", 1)[1].split("/", 1)[0]
        repository = parsed.path.strip("/").removesuffix(".git")
        if (
            parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or parsed.username is not None or parsed.password is not None or "@" in authority
            or parsed.query or parsed.fragment or not REPOSITORY.fullmatch(repository)
        ):
            raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(422, "Choose a credential-free https://github.com/owner/repository URL.") from None
    return "https://github.com/" + repository


class ProductWorkspaceCompatibility:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.identity = runtime.product_identity
        self.schema = self.identity.schema if self.identity else None
        from .product_continuation_restore import ProductWorkspaceRestorer
        self.restorer = ProductWorkspaceRestorer(runtime)

    def repository_bootstrap_available(self) -> bool:
        return callable(getattr(getattr(self.runtime, "product_connections", None), "prepare_repository", None))

    @staticmethod
    def authorize_project_grants(context: dict[str, Any], grants: list[Grant]) -> None:
        if context.get("clone_access") in {"public", None}:
            return
        repository_id = context.get("repository_id")
        if not repository_id or not any(
            grant.kind == "github" and grant.policy.get("product", {}).get("repository_id") == repository_id
            for grant in grants
        ):
            raise HTTPException(403, "Select a current GitHub connection for this environment's repository.")

    def table(self, name: str) -> str:
        if self.schema is None:
            raise HTTPException(503, "Canonical project configuration is unavailable.")
        return identifier(self.schema) + "." + identifier(name)

    async def catalog(self, principal: ProductPrincipal) -> list[dict[str, Any]]:
        if self.identity is None:
            raise HTTPException(503, "Canonical environment configuration is unavailable.")
        current = await self.identity.require_current(principal.grant)
        async with self.runtime.database.sessions() as db:
            rows = (await db.execute(text(f"""
                SELECT e.id,e.recipe,e.revision,e.enabled,e.activate_on_ready,e.active_build,e.is_default,
                    b.recipe AS build_recipe,b.phase,b.snapshot_id,b.commit_sha,b.sandbox_provider,
                    b.revision AS build_revision
                FROM {self.table('environments')} e
                LEFT JOIN {self.table('environment_builds')} b ON b.id=e.active_build
                WHERE (:admin=1 OR e.activate_on_ready=1 OR (e.enabled=1 AND e.active_build!=''))
                ORDER BY e.is_default DESC,e.updated_at DESC,e.id LIMIT 1001
            """), {"admin": int(current.role == "admin")})).mappings()
            catalog = []
            for row in rows:
                if len(catalog) >= 1000:
                    raise HTTPException(503, "The environment catalog exceeds the supported page size.")
                recipe = json.loads(row["build_recipe"] or row["recipe"])
                ready = bool(row["enabled"] and row["phase"] == "ready" and row["snapshot_id"]
                             and row["sandbox_provider"] == "modal")
                catalog.append({
                    "id": row["id"], "name": str(recipe["name"])[:200],
                    "repository": recipe["repository"], "repository_id": recipe.get("repository_id"),
                    "revision": row["revision"], "enabled": bool(row["enabled"]),
                    "active_build": row["active_build"], "is_default": bool(row["is_default"]),
                    "ready": ready, "phase": row["phase"] or "not_prepared",
                    "prepared_build": {
                        "id": row["active_build"], "revision": row["build_revision"],
                        "commit_sha": row["commit_sha"], "sandbox_provider": row["sandbox_provider"],
                    } if row["active_build"] else None,
                })
        await self.identity.require_current(principal.grant)
        return catalog

    async def _selection(
        self, db: AsyncSession, selection: dict[str, Any], *, require_ready: bool,
    ) -> dict[str, Any]:
        repo_url = canonical_repository(str(selection.get("repo_url") or ""))
        repository_id = selection.get("github_repository_id")
        if repository_id is not None and (type(repository_id) is not int or repository_id <= 0):
            raise HTTPException(422, "Use the canonical permanent repository ID.")
        requested = str(selection.get("environment_id") or "auto")
        if requested not in {"auto", "none"} and not BUILD_ID.fullmatch(requested):
            raise HTTPException(422, "Choose an existing product environment.")
        explicit_build = str(selection.get("environment_build_id") or "")
        if explicit_build not in {"", "none"} and not BUILD_ID.fullmatch(explicit_build):
            raise HTTPException(422, "The saved environment build reference is invalid.")
        environment = None
        build = None
        if explicit_build not in {"", "none"}:
            build = (await db.execute(text(
                f"SELECT {BUILD_COLUMNS} FROM {self.table('environment_builds')} WHERE id=:id"
            ), {"id": explicit_build})).mappings().first()
            if build is None:
                raise HTTPException(409, "The saved environment build is missing. Select a current environment.")
            environment = (await db.execute(text(
                f"SELECT * FROM {self.table('environments')} WHERE id=:id"
            ), {"id": build["environment_id"]})).mappings().first()
            if environment is None or (requested not in {"auto", "none"} and requested != build["environment_id"]):
                raise HTTPException(409, "The saved build no longer belongs to the selected environment.")
        elif requested != "none" and explicit_build != "none":
            if requested != "auto":
                candidates = list((await db.execute(text(
                    f"SELECT * FROM {self.table('environments')} WHERE id=:id"
                ), {"id": requested})).mappings())
                if not candidates:
                    raise HTTPException(409, "The selected environment no longer exists.")
            else:
                candidates = list((await db.execute(text(
                    f"SELECT * FROM {self.table('environments')} WHERE enabled=1 OR activate_on_ready=1 "
                    "ORDER BY is_default DESC,updated_at DESC,id LIMIT 1001"
                ))).mappings())
                if len(candidates) > 1000:
                    raise HTTPException(409, "Select an explicit environment from the product catalog.")
            build_ids = [candidate["active_build"] for candidate in candidates if candidate["active_build"]]
            builds = {}
            if build_ids:
                query = text(
                    f"SELECT {BUILD_COLUMNS} FROM {self.table('environment_builds')} WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                builds = {row["id"]: row for row in (await db.execute(query, {"ids": build_ids})).mappings()}
            for candidate in candidates:
                candidate_build = builds.get(candidate["active_build"])
                try:
                    recipe = json.loads(candidate_build["recipe"] if candidate_build else candidate["recipe"])
                except (ValueError, TypeError):
                    raise HTTPException(409, "The environment recipe is invalid; rebuild it in Environments.") from None
                match = (
                    repository_id == recipe.get("repository_id") if repository_id is not None
                    else bool(repo_url and repo_url.casefold() ==
                              canonical_repository("https://github.com/" + recipe["repository"]).casefold())
                )
                if requested != "auto" or match or (not repo_url and repository_id is None and candidate["is_default"]):
                    if candidate_build is not None and candidate_build["environment_id"] != candidate["id"]:
                        raise HTTPException(409, "The selected environment has an invalid active build reference.")
                    environment, build = candidate, candidate_build
                    break
        recipe: dict[str, Any] = {}
        if environment is not None:
            if (explicit_build in {"", "none"} and not environment["enabled"]
                    and not environment["activate_on_ready"]):
                raise HTTPException(409, "This environment is disabled. Enable a validated environment first.")
            if require_ready and (build is None or build["phase"] != "ready" or not build["snapshot_id"]):
                raise HTTPException(409, "The selected environment is still preparing. Wait for its validated build.")
            if build is not None:
                if build["phase"] != "ready" or build.get("sandbox_provider", "modal") != "modal":
                    raise HTTPException(409, "Prepare this environment for Modal before native execution.")
                try:
                    recipe = json.loads(build.get("resolved_recipe") or build["recipe"])
                except (ValueError, TypeError):
                    raise HTTPException(409, "The validated environment recipe cannot be read.") from None
                built_repo = canonical_repository("https://github.com/" + str(recipe.get("repository") or ""))
                built_id = recipe.get("repository_id")
                if repo_url and repo_url.casefold() != built_repo.casefold():
                    raise HTTPException(409, "The repository no longer matches the selected environment.")
                if repository_id is not None and built_id != repository_id:
                    raise HTTPException(409, "The permanent repository identity changed.")
                repo_url, repository_id = built_repo, built_id
        result = {
            "version": 1, "source_schema": self.schema,
            "selection": {"repo_url": canonical_repository(str(selection.get("repo_url") or "")),
                          "github_repository_id": selection.get("github_repository_id"), "environment_id": requested},
            "repo_url": repo_url, "repository_id": repository_id,
            "environment_id": environment["id"] if environment else "none",
            "environment_build_id": build["id"] if build else None,
            "environment_revision": build["revision"] if build else None,
            "build_fingerprint": build_fingerprint(build) if build else None,
            "clone_access": recipe.get("clone_access", "public"),
            "pinned_source_build": explicit_build not in {"", "none"},
            "commit_sha": build["commit_sha"] if build else None,
            "working_dir": "/workspace/repo" if repo_url else "/workspace",
        }
        result["context_fingerprint"] = fingerprint(result)
        return result

    async def automation_blockers(self, db: AsyncSession, owner_id: str, definition: Any) -> list[str]:
        selection = {
            name: getattr(definition, name, None)
            for name in ("repo_url", "github_repository_id", "environment_id")
        } if not isinstance(definition, dict) else definition
        try:
            context = await self._selection(db, selection, require_ready=True)
            if context["repo_url"] and not context["environment_build_id"] and not self.repository_bootstrap_available():
                return ["Prepare and select a validated environment for this repository before enabling automation."]
        except HTTPException as error:
            return [str(error.detail)]
        return []

    async def capture_workspace_context(
        self, db: AsyncSession, principal: ProductPrincipal, selection: dict[str, Any],
    ) -> dict[str, Any]:
        if self.identity is None:
            raise HTTPException(503, "Current product authority is unavailable.")
        await self.identity.authorize_admission(db, principal.grant)
        context = await self._selection(db, selection, require_ready=True)
        if (
            context["repo_url"] and not context["environment_build_id"] and "environment_build_id" not in selection
            and not self.repository_bootstrap_available()
        ):
            raise HTTPException(409, "Prepare and select a validated repository environment before native execution.")
        return context

    async def validate_workspace_context(
        self, db: AsyncSession, principal: ProductPrincipal, selection: dict[str, Any], saved: dict[str, Any],
    ) -> dict[str, Any]:
        fresh = await self.capture_workspace_context(db, principal, selection)
        # Routine validated-build refreshes are adopted per occurrence. Changing
        # the selected permanent repository/environment requires explicit enable.
        if saved.get("selection") != fresh["selection"] or saved.get("repository_id") != fresh["repository_id"]:
            raise HTTPException(409, "The saved repository/environment selection changed. Review and enable again.")
        return fresh

    async def bind_automation_turn(
        self, db: AsyncSession, session: Session, turn: Turn, context: dict[str, Any],
    ) -> None:
        if turn.session_id != session.id:
            raise ValueError("Execution context must belong to its admitted session.")
        digest = fingerprint(context)
        existing = await db.get(ProductExecutionContext, turn.id)
        if existing is not None:
            if existing.fingerprint != digest or existing.actor_id != turn.actor_id:
                raise HTTPException(409, "The native turn already has a different execution context.")
            return
        db.add(ProductExecutionContext(
            turn_id=turn.id, actor_id=turn.actor_id, descriptor=self.runtime.security.seal(context),
            fingerprint=digest,
        ))

    async def turn_context(self, scope: RunScope) -> dict[str, Any] | None:
        await self.runtime.database.authorize_scope(scope)
        async with self.runtime.database.sessions() as db:
            row = await db.get(ProductExecutionContext, scope.turn_id)
            if row is None:
                return None
            if row.actor_id != scope.actor_id:
                raise PermissionError("The project context belongs to a different actor.")
            context = json.loads(self.runtime.security.unseal(row.descriptor))
            if fingerprint(context) != row.fingerprint:
                raise PermissionError("The project context failed integrity verification.")
            if context.get("environment_build_id"):
                current = (await db.execute(text(
                    f"SELECT {BUILD_COLUMNS} FROM {self.table('environment_builds')} WHERE id=:id"
                ), {"id": context["environment_build_id"]})).mappings().first()
                enabled = await db.scalar(text(
                    f"SELECT enabled FROM {self.table('environments')} WHERE id=:id"
                ), {"id": context["environment_id"]})
                if (
                    current is None or (not enabled and not context.get("pinned_source_build"))
                    or current["phase"] != "ready"
                    or build_fingerprint(current) != context["build_fingerprint"]
                ):
                    raise PermissionError("The pinned project build changed or was disabled.")
        await self.runtime.database.authorize_scope(scope)
        return context

    async def prepare(self, prepared: PreparedTurn, *, recover_workspace: bool = False) -> PreparedTurn:
        """Pin a session's inherited project references on every native turn."""
        scope = prepared.scope
        await self.runtime.database.authorize_scope(scope)
        async with self.runtime.database.sessions.begin() as db:
            existing = await db.get(ProductExecutionContext, scope.turn_id)
            if existing is None:
                origin = await db.scalar(select(ProductContinuation).where(
                    ProductContinuation.native_session_id == scope.session_id,
                ))
                if origin is not None:
                    plan = json.loads(self.runtime.security.unseal(origin.workspace))
                    context = plan["context"]
                    session = await db.get(Session, scope.session_id)
                    turn = await db.get(Turn, scope.turn_id)
                    if session is None or turn is None:
                        raise PermissionError("The project session no longer exists.")
                    await self.bind_automation_turn(db, session, turn, context)
                else:
                    previous = await db.scalar(
                        select(ProductExecutionContext).join(Turn, Turn.id == ProductExecutionContext.turn_id)
                        .where(Turn.session_id == scope.session_id)
                        .order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1)
                    )
                    if previous is not None:
                        context = json.loads(self.runtime.security.unseal(previous.descriptor))
                        if fingerprint(context) != previous.fingerprint:
                            raise PermissionError("The earlier project selection failed integrity verification.")
                        session, turn = await db.get(Session, scope.session_id), await db.get(Turn, scope.turn_id)
                        if session is None or turn is None:
                            raise PermissionError("The project session no longer exists.")
                        await self.bind_automation_turn(db, session, turn, context)
        context = await self.turn_context(scope)
        if context is None:
            if recover_workspace:
                from .workspace_recovery import prepare_workspace
                return await prepare_workspace(self.runtime, prepared)
            return prepared
        self.authorize_project_grants(context, scope.grants)
        materialized = dict(context)
        if context.get("environment_build_id"):
            async with self.runtime.database.sessions() as db:
                image = await db.scalar(text(
                    f"SELECT snapshot_id FROM {self.table('environment_builds')} WHERE id=:id AND phase='ready'"
                ), {"id": context["environment_build_id"]})
            if not image:
                raise PermissionError("The pinned environment image is unavailable.")
            materialized.update(environment_snapshot_id=image, image_source="validated_environment")
        await self.runtime.database.authorize_scope(scope)
        if recover_workspace:
            from .workspace_recovery import prepare_workspace
            prepared = await prepare_workspace(self.runtime, prepared)
        prepared = await self.restorer.prepare(prepared, materialized)
        await self.turn_context(scope)
        self.authorize_project_grants(context, scope.grants)
        return prepared

    async def repository_session(
        self, db: AsyncSession, native_session_id: str, current: ProductPrincipal,
    ) -> Session:
        session = await db.get(Session, native_session_id)
        if session is None or session.deleted:
            raise PermissionError("The project session is no longer accessible.")
        if session.visibility == "private":
            context = await self.runtime.database.execution_context(db, session)
            # Private child presentation does not revoke its Slack owner's
            # inherited shared authority or grant ordinary private sessions.
            if session.owner_id != current.id or (not current.individual and context.visibility != "shared"):
                raise PermissionError("The project session is no longer accessible.")
        return session

    async def repository_scope(
        self, native_session_id: str, grant: ProductGrant, *, turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Memory receives a current, explicit permanent repository identity."""
        if self.identity is None:
            raise PermissionError("Current product authority is unavailable.")
        current = await self.identity.require_current(grant)
        receipt = None
        prepared = None
        saved_preparation = None
        connections = getattr(self.runtime, "product_connections", None)
        async with self.runtime.database.sessions() as db:
            session = await self.repository_session(db, native_session_id, current)
            query = select(ProductExecutionContext).join(Turn, Turn.id == ProductExecutionContext.turn_id).where(
                Turn.session_id == native_session_id,
            )
            if turn_id is not None:
                query = query.where(Turn.id == turn_id, ProductExecutionContext.actor_id == current.id)
            row = await db.scalar(query.order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))
            if row is None:
                return None
            context = json.loads(self.runtime.security.unseal(row.descriptor))
            if fingerprint(context) != row.fingerprint:
                raise PermissionError("The saved repository selection failed integrity verification.")
            if context.get("environment_build_id"):
                build = (await db.execute(text(
                    f"SELECT {BUILD_COLUMNS} FROM {self.table('environment_builds')} WHERE id=:id"
                ), {"id": context["environment_build_id"]})).mappings().first()
                if build is None or build_fingerprint(build) != context["build_fingerprint"]:
                    raise PermissionError("The selected repository build changed.")
            if (
                not context.get("repository_id") and context.get("repo_url")
                and connections is not None and callable(getattr(connections, "repository_receipt", None))
            ):
                turn = await db.get(Turn, row.turn_id)
                if turn is not None and turn.preparation:
                    saved_preparation = turn.preparation
                    prepared = TypeAdapter(PreparedTurn).validate_json(self.runtime.security.unseal(saved_preparation))
                    scope = prepared.scope
                    if (
                        scope.session_id != native_session_id or scope.turn_id != row.turn_id
                        or scope.actor_id != current.id or turn.actor_id != current.id
                        or scope.product_grant != grant or scope.visibility != session.visibility
                        or scope.surface != session.surface
                    ):
                        raise PermissionError("The repository receipt belongs to a different task authority.")
                    await self.runtime.database._check_product_turn(db, turn, grant)
                    if prepared.workspace and prepared.workspace.get("provider") in {"modal", "lambda"}:
                        # The current session computer may belong to a later
                        # turn. Historical memory reads use this exact receipt.
                        receipt = await connections.repository_receipt(
                            db, session_id=native_session_id, workspace_id=prepared.workspace.get("id", ""),
                            workspace_scope=scope.workspace_key, repo_url=context["repo_url"],
                        )
        await self.identity.require_current(grant)
        if context.get("source_session_id") and self.runtime.product_history:
            await self.runtime.product_history.require(current, context["source_session_id"])
        if receipt is not None and prepared is not None and connections is not None:
            await connections.require_scope(prepared.scope)
            if receipt["public"]:
                from app.connector_errors import ConnectorError
                try:
                    info = await connections.public_repository(repository_id=receipt["repository_id"])
                except (ConnectorError, HTTPException):
                    # Repository-memory enrichment is optional. A rate limit
                    # cannot grant access or disable an otherwise ready task.
                    await self.identity.require_current(grant)
                    await connections.require_scope(prepared.scope)
                    return None
                if (
                    not isinstance(info, dict) or info.get("id") != receipt["repository_id"]
                    or info.get("private") is not False
                ):
                    return None
            elif not any(
                item.id == receipt["connection_id"] and item.revision == receipt["connection_revision"]
                and item.policy.get("product", {}).get("repository_id") == receipt["repository_id"]
                for item in prepared.scope.grants
            ):
                raise PermissionError("The repository receipt's original account is no longer selected.")
            current = await self.identity.require_current(grant)
            await connections.require_scope(prepared.scope)
            async with self.runtime.database.sessions() as db:
                session = await self.repository_session(db, native_session_id, current)
                turn = await db.get(Turn, prepared.scope.turn_id)
                if (
                    session is None or session.deleted or turn is None or turn.preparation != saved_preparation
                    or session.visibility != prepared.scope.visibility or session.surface != prepared.scope.surface
                ):
                    raise PermissionError("The historical repository task changed during authorization.")
                await self.runtime.database._check_product_turn(db, turn, grant)
            if context.get("source_session_id") and self.runtime.product_history:
                await self.runtime.product_history.require(current, context["source_session_id"])
            context = {**context, "repository_id": receipt["repository_id"], "repo_url": receipt["repo_url"]}
        if not context.get("repository_id"):
            return None
        return {key: context.get(key) for key in (
            "source_schema", "source_session_id", "repository_id", "repo_url", "environment_id",
            "environment_build_id", "environment_revision", "context_fingerprint",
        )}
