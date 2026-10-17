"""Native request handles over the canonical vault; no legacy execution calls."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from app.credentials import CredentialRequest, ListCredentials, Resolve, RunCredential
from app.requester_identity import same_requester
from fastapi import HTTPException, Request
from pydantic import SecretStr, TypeAdapter
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from sqlalchemy import select, text

from .contracts import RunScope
from .models import Effect, Session, Turn, User, now
from .product_connection_store import ConnectionSnapshot
from .product_credential_models import ProductCredentialRequest as AccessRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .product_identity import ProductPrincipal
    from .product_secrets import ProductSecrets


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_arguments(value: Any) -> dict[str, Any]:
    """The SDK accepts flattened single-model arguments and their wrapped form."""
    if not isinstance(value, dict):
        raise TypeError("Expected credential arguments.")
    if set(value) == {"request"} and isinstance(value["request"], dict):
        return value["request"]
    return value


class ProductCredentialExecution:
    def __init__(self, vault: ProductSecrets):
        self.vault, self.runtime = vault, vault.runtime
        self.database, self.identity = vault.runtime.database, vault.product.identity
        self.enabled = False

    def table(self, name: str) -> str:
        if name not in {"provider_secrets", "credential_audit"}:
            raise ValueError("Unexpected canonical credential table.")
        return self.identity._table(name)

    async def writer(self, db: AsyncSession) -> None:
        if db.get_bind().dialect.name != "postgresql":
            raise HTTPException(503, "Credential execution requires canonical PostgreSQL admission.")
        # Same writer namespace as canonical Credentials and account mutations.
        await db.execute(text(
            "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": self.identity.schema})

    async def equivalent(self, db: AsyncSession, owner: str, actor: str) -> bool:
        return await db.run_sync(lambda session: same_requester(
            ConnectionSnapshot(session, self.identity.schema), owner, actor,
        ))

    async def current_actor(self, db: AsyncSession, principal: ProductPrincipal) -> User:
        actor = await db.get(User, principal.id)
        if actor is None or not actor.enabled or actor.auth_version != principal.native_auth_version:
            raise HTTPException(403, "Open the current workspace before resolving its secure request.")
        # This transient admission value came from the live canonical check in
        # this transaction, never from the cached user's role/profile.
        cast(Any, actor).product_grant = principal.grant
        return actor

    async def authorize(
        self, db: AsyncSession, scope: RunScope, run_id: str | None = None, *, direct: bool = False,
    ) -> ProductPrincipal:
        principal = await self.identity.authorize_admission(db, scope.product_grant)
        actor, turn = await db.get(User, scope.actor_id), await db.get(Turn, scope.turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise HTTPException(403, "This credential execution is unavailable.")
        await self.database._check_product_turn(db, turn, scope.product_grant)
        session = await self.database.allowed_session(db, scope.session_id, actor)
        if (
            principal.id != scope.actor_id or turn.actor_id != principal.id or turn.session_id != session.id
            or session.active_turn != turn.id or session.status != "running" or turn.status != "running"
            or session.visibility != scope.visibility or session.surface != scope.surface
            or (direct and (run_id != turn.id or turn.parent_turn is not None))
        ):
            raise HTTPException(403, "The original requester no longer owns this execution.")
        grants = await self.database.scope_grants(db, scope, session)
        if [(grant.id, grant.revision) for grant in grants] != [(grant.id, grant.revision) for grant in scope.grants]:
            raise HTTPException(403, "Connected account authority changed.")
        return principal

    async def secret(self, db: AsyncSession, identity: str) -> dict | None:
        row = (await db.execute(text(
            f"SELECT * FROM {self.table('provider_secrets')} WHERE id=:id"
        ), {"id": identity})).mappings().first()
        return dict(row) if row else None

    async def visible(self, db: AsyncSession, secret: dict, actor_id: str, session_id: str) -> bool:
        if secret["lifetime"] == "session" and secret["root_id"] != "native:" + session_id:
            from .product_continuation_models import ProductContinuation
            continuation = await db.scalar(select(ProductContinuation).where(
                ProductContinuation.native_session_id == session_id,
                ProductContinuation.source_schema == self.identity.schema,
            ))
            if continuation is None:
                return False
            root = await db.scalar(text(
                f"SELECT root_id FROM {self.identity._table('run_roots')} WHERE run_id=:id"
            ), {"id": continuation.source_session_id})
            if secret["root_id"] != (root or continuation.source_session_id):
                return False
        if secret["scope"] == "organization":
            return True
        session = await db.get(Session, session_id)
        context = await self.database.execution_context(db, session) if session else None
        return bool(context and context.visibility == "private" and context.surface == "web") and await self.equivalent(
            db, secret["owner_id"], actor_id,
        )

    async def permitted(self, db: AsyncSession, secret: dict, actor_id: str, session_id: str) -> bool:
        return self.vault.credentials.status(secret) == "active" and await self.visible(db, secret, actor_id, session_id)

    @staticmethod
    def public(row: AccessRequest) -> dict:
        capability = json.loads(row.capability)
        return {
            "request_id": row.id, "generation": row.generation, "status": row.status,
            "revision": row.secret_revision, "session_id": row.session_id,
            **{key: capability[key] for key in (
                "provider", "name", "format", "env_var", "reason", "setup_url", "setup_instructions", "input_fields",
            )},
        }

    async def inventory(self, scope: RunScope, args: ListCredentials) -> dict:
        async with self.database.sessions.begin() as db:
            await self.authorize(db, scope)
            rows = [dict(row) for row in (await db.execute(text(
                f"SELECT * FROM {self.table('provider_secrets')} WHERE revoked_at='' ORDER BY created_at DESC"
            ))).mappings()]
            allowed = [row for row in rows if await self.visible(db, row, scope.actor_id, scope.session_id)]
            pending = list(await db.scalars(select(AccessRequest).where(
                AccessRequest.session_id == scope.session_id, AccessRequest.actor_id == scope.actor_id,
                AccessRequest.status == "pending",
            )))
            metadata = lambda row: self.vault.credentials.metadata(row, scope.actor_id, False)
            return {
                "credentials": [metadata(row) for row in allowed if (
                    (not args.provider or row["provider"] == args.provider)
                    and (not args.name or row["name"] == args.name.strip().lower())
                )],
                "credential_sources": [metadata(row) for row in allowed if (
                    row["name"] == "1password-shared" and self.vault.credentials.status(row) == "active"
                )],
                "pending_requests": [self.public(row) for row in pending],
                "discovery": {"filter_applied": bool(args.provider or args.name),
                              "available_connections": sum(self.vault.credentials.status(row) == "active" for row in allowed),
                              "capability_verification": "not_assessed"},
            }

    async def source_lookup(self, db: AsyncSession, scope: RunScope, args: CredentialRequest, rows: list[dict]) -> dict | None:
        sources = [value for value in rows if (
            value["provider"] == "generic" and value["name"] == "1password-shared"
            and await self.permitted(db, value, scope.actor_id, scope.session_id)
        )]
        if not sources:
            return None
        checked = {(item.secret_id, item.revision) for item in args.source_checks}
        used = set()
        for effect in await db.scalars(select(Effect).where(
            Effect.turn_id == scope.turn_id, Effect.actor_id == scope.actor_id,
            Effect.tool_name == "credentials_run", Effect.state == "confirmed",
        )):
            try:
                arguments = json.loads(self.runtime.security.unseal(effect.arguments))
                handles = RunCredential.model_validate(request_arguments(arguments)).request_ids
            except (ValueError, TypeError, KeyError):
                continue
            for handle in handles:
                row = await db.get(AccessRequest, handle)
                if row and row.actor_id == scope.actor_id and row.session_id == scope.session_id:
                    used.add((row.secret_id, row.secret_revision))
        if checked and not checked.issubset(used):
            raise HTTPException(422, "A credential source check needs a confirmed credentials_run use in this turn.")
        missing = [value for value in sources if (value["id"], value["revision"]) not in checked]
        if not missing:
            return None
        return {
            "status": "lookup_required",
            "credential_sources": [self.vault.credentials.metadata(value, scope.actor_id, False) for value in missing],
            "instruction": "Inspect available Shared vault sources through credentials_run before requesting another key. "
            "If lookup fails, report only the observed source ID, revision, and failure outcome; never include a secret.",
        }

    async def request(
        self, scope: RunScope, run_id: str, tool_call_id: str, args: CredentialRequest,
    ) -> dict:
        if not tool_call_id or len(tool_call_id) > 200:
            raise HTTPException(422, "A native tool-call identity is required.")
        if args.format == "aws-sso":
            raise HTTPException(409, "Use the interactive AWS login flow; an AWS token cannot be requested as a generic secret.")
        document = args.model_dump(mode="json")
        fingerprint = digest(document)
        async with self.database.sessions.begin() as db:
            await self.writer(db)
            principal = await self.authorize(db, scope, run_id, direct=True)
            row = await db.scalar(select(AccessRequest).where(
                AccessRequest.session_id == scope.session_id, AccessRequest.actor_id == principal.id,
                AccessRequest.turn_id == scope.turn_id, AccessRequest.request_key == args.request_key,
            ).with_for_update())
            if row is not None:
                if row.fingerprint != fingerprint or (row.status == "pending" and row.tool_call_id != tool_call_id):
                    raise HTTPException(409, "This credential request identity already has different content.")
                return self.public(row)
            rows = [dict(value) for value in (await db.execute(text(
                f"SELECT * FROM {self.table('provider_secrets')} WHERE revoked_at='' ORDER BY created_at DESC"
            ))).mappings()]
            matches = [value for value in rows if (
                self.vault.credentials.matching(value, document)
                and (not args.secret_id or value["id"] == args.secret_id)
                and await self.permitted(db, value, principal.id, scope.session_id)
            )]
            if args.secret_id and not matches:
                raise HTTPException(403, "The selected saved credential is not available for this request.")
            selected = matches[0] if matches else None
            if selected is None:
                lookup = await self.source_lookup(db, scope, args, rows)
                if lookup:
                    return lookup
            row = AccessRequest(
                id=uuid4().hex, session_id=scope.session_id, turn_id=scope.turn_id, actor_id=principal.id,
                authority_version=principal.authority_version, tool_call_id=tool_call_id,
                request_key=args.request_key, fingerprint=fingerprint, capability=json.dumps(document),
                status="provided" if selected else "pending", secret_id=selected["id"] if selected else None,
                secret_revision=selected["revision"] if selected else 0,
                generation=0, created_at=now(), updated_at=now(),
            )
            db.add(row)
            await db.flush()
            await self.audit(db, principal.id, row.secret_id or "", "native reused" if selected else "native requested", scope.session_id)
            return self.public(row)

    async def audit(self, db: AsyncSession, actor: str, secret: str, action: str, session: str) -> None:
        await db.execute(text(
            f"INSERT INTO {self.table('credential_audit')}(actor_id,secret_id,action,run_id,created_at) "
            "VALUES(:actor,:secret,:action,:session,:stamp)"
        ), {"actor": actor, "secret": secret, "action": action, "session": "native:" + session, "stamp": now()})

    async def authorized_request(
        self, db: AsyncSession, scope: RunScope, request_id: str,
    ) -> tuple[AccessRequest, dict]:
        await self.authorize(db, scope)
        row = await db.get(AccessRequest, request_id)
        if (
            row is None or row.session_id != scope.session_id or row.actor_id != scope.actor_id
            or scope.product_grant is None or row.authority_version != scope.product_grant.authority_version
            or row.status != "provided" or not row.secret_id
        ):
            raise HTTPException(403, "Request this credential for the current requester and session.")
        secret = await self.secret(db, row.secret_id)
        if (
            secret is None or secret["revision"] != row.secret_revision
            or not self.vault.credentials.matching(secret, json.loads(row.capability))
            or not await self.permitted(db, secret, scope.actor_id, scope.session_id)
        ):
            raise HTTPException(403, "The saved credential changed or expired. Request its current revision.")
        return row, secret

    async def webhook_secret(
        self, scope: RunScope, run_id: str, request_id: str, *, db: AsyncSession, shared_sink: bool = True,
    ) -> SecretStr:
        await self.authorize(db, scope, run_id, direct=True)
        _, secret = await self.authorized_request(db, scope, request_id)
        if shared_sink and secret["scope"] != "organization":
            raise HTTPException(403, "Shared webhook receivers require organization credentials.")
        if (
            secret["provider"] != "generic" or secret["name"] != "webhook-signing-secret"
            or secret["format"] != "env" or secret["lifetime"] != "persistent" or secret["expires_at"]
        ):
            raise HTTPException(422, "Use non-expiring persistent generic webhook-signing-secret credentials.")
        try:
            values = json.loads(self.vault.security.decrypt(secret["encrypted"]))
            value = values.get("WEBHOOK_SECRET") if isinstance(values, dict) else None
            if not isinstance(value, str) or not 16 <= len(value) <= 512:
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, "The credential must contain WEBHOOK_SECRET with 16 to 512 characters.") from None
        return SecretStr(value)

    async def materialize(self, scope: RunScope, request_ids: list[str]) -> list[dict]:
        async with self.database.sessions.begin() as db:
            accepted = [await self.authorized_request(db, scope, identity) for identity in request_ids]
            for _, secret in accepted:
                if secret["provider"] != "generic" or secret["name"] == "slack-personal" or secret["format"] == "aws-sso":
                    raise HTTPException(403, "This credential is restricted to its server-side service.")
            # All handles pass live authorization before any value is decrypted.
            return [{
                "request_id": row.id, "revision": row.secret_revision, "format": secret["format"],
                "env_var": secret["env_var"], "name": secret["name"],
                "value": self.vault.security.decrypt(secret["encrypted"]),
            } for row, secret in accepted]

    async def pending(self, principal: ProductPrincipal, session_id: str) -> dict:
        async with self.database.sessions.begin() as db:
            await self.identity.authorize_admission(db, principal.grant)
            actor = await self.current_actor(db, principal)
            session = await self.database.allowed_session(db, session_id, actor)
            if session.status != "waiting" or not session.active_turn:
                return {"requests": []}
            rows = list(await db.scalars(select(AccessRequest).where(
                AccessRequest.session_id == session_id, AccessRequest.turn_id == session.active_turn,
                AccessRequest.status == "pending",
            ).order_by(AccessRequest.created_at)))
            return {"requests": [self.public(row) for row in rows if (
                await self.equivalent(db, principal.id, row.actor_id)
                or await self.equivalent(db, row.actor_id, principal.id)
                or principal.role == "admin"
            )]}

    async def describe(self, principal: ProductPrincipal, request_id: str) -> dict:
        async with self.database.sessions.begin() as db:
            await self.identity.authorize_admission(db, principal.grant)
            actor = await self.current_actor(db, principal)
            row = await db.get(AccessRequest, request_id)
            if row is None:
                raise HTTPException(404, "Native credential request not found.")
            session = await self.database.allowed_session(db, row.session_id, actor)
            own = await self.equivalent(db, principal.id, row.actor_id) or await self.equivalent(db, row.actor_id, principal.id)
            if not own and principal.role != "admin":
                raise HTTPException(404, "Native credential request not found.")
            capability = json.loads(row.capability)
            choices = []
            for secret in (await db.execute(text(
                f"SELECT * FROM {self.table('provider_secrets')} WHERE revoked_at='' ORDER BY created_at DESC"
            ))).mappings():
                if self.vault.credentials.matching(secret, capability) and await self.permitted(
                    db, dict(secret), principal.id, row.session_id,
                ):
                    choices.append(self.vault.credentials.metadata(secret, principal.id, principal.role == "admin"))
            context = await self.database.execution_context(db, session)
            private = context.visibility == "private" and context.surface == "web"
            scopes = (["personal"] if private and own and (principal.individual or self.vault.security.local_preview()) else [])
            if principal.role == "admin":
                scopes.append("organization")
            return {**self.public(row), "credentials": choices, "scopes": scopes,
                    "lifetimes": ["session", "persistent"], "can_resolve": row.status == "pending",
                    "note": "" if private else "Personal credentials are restricted to private web sessions. "
                    "Use existing organization access or ask an administrator to supply it for this session."}

    async def resolve(
        self, request: Request, principal: ProductPrincipal, request_id: str, body: Resolve,
    ) -> dict:
        resolution = body.model_dump(mode="json", exclude={"value"})
        resolution["value_hash"] = hashlib.sha256(body.value.get_secret_value().encode()).hexdigest()
        resolution["actor_id"] = principal.id
        fingerprint = digest(resolution)
        async with self.database.sessions.begin() as db:
            await self.writer(db)
            await self.identity.authorize_admission(db, principal.grant)
            row = await db.scalar(select(AccessRequest).where(AccessRequest.id == request_id).with_for_update())
            if row is None:
                raise HTTPException(404, "Native credential request not found.")
            actor = await self.current_actor(db, principal)
            await self.database.allowed_session(db, row.session_id, actor)
            own = await self.equivalent(db, principal.id, row.actor_id) or await self.equivalent(db, row.actor_id, principal.id)
            if not own and principal.role != "admin":
                raise HTTPException(403, "Only the requester or an organization administrator may resolve this request.")
            if body.generation != row.generation:
                raise HTTPException(409, "This request changed. Reload its secure form.")
            if row.status != "pending":
                if not row.resolution_fingerprint or not hmac.compare_digest(row.resolution_fingerprint, fingerprint):
                    raise HTTPException(409, "This request already has a different resolution.")
            else:
                turn = await db.get(Turn, row.turn_id)
                session = await db.scalar(select(Session).where(Session.id == row.session_id).with_for_update())
                if turn is None or session is None or turn.status != "waiting" or session.active_turn != turn.id:
                    raise HTTPException(409, "Wait for this task's secure request, or reopen the current task.")
                original = await self.identity.authorize_admission(
                    db, await self.database._saved_product_grant(db, turn),
                )
                if original.id != row.actor_id or original.authority_version != row.authority_version:
                    raise HTTPException(403, "The requesting task's original authority changed.")
                self.pending_call(turn, row)
                selected = None
                if body.decision == "provide":
                    capability = json.loads(row.capability)
                    if body.secret_id:
                        selected = await self.secret(db, body.secret_id)
                    else:
                        if body.scope != "organization" and not own:
                            raise HTTPException(403, "Only the requester may supply personal credentials.")
                        if body.scope is None:
                            raise HTTPException(422, "Choose credential sharing before saving.")
                        context = await self.database.execution_context(db, session)
                        if body.scope != "organization" and (context.visibility != "private" or context.surface != "web"):
                            raise HTTPException(403, "Personal credentials are restricted to private web sessions.")
                        from .product_secrets import ProductSaveSecret

                        saved = ProductSaveSecret(
                            **{key: capability[key] for key in ("provider", "name", "format", "env_var")},
                            label=body.label.strip() or capability["name"] or capability["provider"],
                            scope=body.scope, lifetime=body.lifetime, expires_at=body.expires_at,
                            value=body.value, client_id=f"native-request-{row.id}-{row.generation}",
                            root_id="native:" + row.session_id if body.lifetime == "session" or body.scope == "session" else "",
                        )
                        try:
                            fields = json.loads(body.value.get_secret_value()) if capability["input_fields"] else {}
                        except (ValueError, TypeError):
                            raise HTTPException(422, "Supply each required credential field.") from None
                        if not isinstance(fields, dict) or any(
                            item["required"] and (not isinstance(fields.get(item["name"]), str) or not fields[item["name"]].strip())
                            for item in capability["input_fields"]
                        ):
                            raise HTTPException(422, "Supply each required credential field.")
                        selected = await self.insert_secret(db, principal, row.session_id, saved)
                    if (
                        selected is None or not self.vault.credentials.matching(selected, capability)
                        or not await self.permitted(db, selected, row.actor_id, row.session_id)
                        or (selected["scope"] != "organization" and (not own or not await self.equivalent(db, selected["owner_id"], principal.id)))
                    ):
                        raise HTTPException(403, "Those credentials are unavailable to this requester and session.")
                row.status = "provided" if selected else "declined"
                row.secret_id = selected["id"] if selected else None
                row.secret_revision = selected["revision"] if selected else 0
                row.resolution_fingerprint, row.updated_at = fingerprint, now()
                await self.audit(db, principal.id, row.secret_id or "", "native " + row.status, row.session_id)
            turn_id, session_id = row.turn_id, row.session_id
        # Committed resolution is retryable if resume/HTTP acknowledgement is lost.
        return await self.resume_resolved(turn_id, session_id)

    async def insert_secret(self, db, principal, session_id, body) -> dict:
        if body.scope == "organization" and principal.role != "admin":
            raise HTTPException(403, "Only an administrator may save organization credentials.")
        if body.scope != "organization" and not (principal.individual or self.vault.security.local_preview()):
            raise HTTPException(403, "Sign in individually to save personal credentials.")
        value = self.vault.credentials.validated_value(body)
        fields = {key: getattr(body, key) for key in (
            "provider", "name", "format", "env_var", "label", "scope", "lifetime", "expires_at", "client_id",
        )}
        fields.update(id=uuid4().hex, owner_id=principal.id, root_id=body.root_id,
                      encrypted=self.vault.security.encrypt(value), created_at=now())
        await db.execute(text(
            f"INSERT INTO {self.table('provider_secrets')}({','.join(fields)}) "
            f"VALUES({','.join(':' + key for key in fields)})"
        ), fields)
        await self.audit(db, principal.id, fields["id"], "saved " + body.scope + " " + body.lifetime, session_id)
        result = await self.secret(db, fields["id"])
        assert result is not None
        return result

    def pending_call(self, turn: Turn, row: AccessRequest) -> None:
        if not turn.conversation:
            raise HTTPException(409, "The task has not persisted its secure request yet.")
        pending = ConversationTypeAdapter.validate_json(
            self.runtime.security.unseal(turn.conversation),
        ).deferred_tool_requests
        call = next((call for call in pending.calls if call.tool_call_id == row.tool_call_id), None) if pending else None
        if call is None or call.tool_name != "credentials_request":
            raise HTTPException(409, "This request is not the task's current deferred tool call.")
        try:
            expected = CredentialRequest.model_validate(request_arguments(call.args_as_dict()))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(409, "The deferred credential request changed.") from None
        if digest(expected.model_dump(mode="json")) != row.fingerprint:
            raise HTTPException(409, "The deferred credential request changed.")

    async def resolved_calls(self, db: AsyncSession, turn: Turn, actor: User) -> dict[str, dict]:
        """Seed the existing answers route with server-validated credential results.

        The caller still validates every ordinary question/approval and uses the
        SDK's build_results. User JSON never supplies credential tool results.
        """
        if actor.id != turn.actor_id:
            raise HTTPException(403, "Only the original requester may resume this task.")
        await self.identity.authorize_admission(db, await self.database._saved_product_grant(db, turn))
        await self.database.allowed_session(db, turn.session_id, actor)
        pending = ConversationTypeAdapter.validate_json(
            self.runtime.security.unseal(turn.conversation),
        ).deferred_tool_requests if turn.conversation else None
        identities = {call.tool_call_id for call in pending.calls if call.tool_name == "credentials_request"} if pending else set()
        calls = {}
        for row in await db.scalars(select(AccessRequest).where(
            AccessRequest.turn_id == turn.id, AccessRequest.actor_id == actor.id,
            AccessRequest.status.in_(["provided", "declined", "satisfied"]),
        )):
            if row.tool_call_id not in identities:
                continue
            self.pending_call(turn, row)
            status = row.status
            if status == "provided":
                secret = await self.secret(db, row.secret_id or "")
                if secret is None or secret["revision"] != row.secret_revision or not await self.permitted(
                    db, secret, actor.id, turn.session_id,
                ):
                    status = "unavailable"
            calls[row.tool_call_id] = {"request_id": row.id, "status": status, "revision": row.secret_revision}
        return calls

    async def resume_resolved(self, turn_id: str, session_id: str) -> dict:
        async with self.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not turn.conversation:
                raise HTTPException(409, "The requesting task is unavailable.")
            principal = await self.identity.authorize_admission(
                db, await self.database._saved_product_grant(db, turn),
            )
            actor = await self.current_actor(db, principal)
            pending = ConversationTypeAdapter.validate_json(
                self.runtime.security.unseal(turn.conversation)
            ).deferred_tool_requests
            if pending is None:
                raise HTTPException(409, "The task has no deferred request.")
            from .product_slack_controls import stored_replies
            calls, approvals = await stored_replies(self.runtime, db, turn)
            calls.update(await self.resolved_calls(db, turn, actor))
            try:
                results = pending.build_results(calls=calls, approvals=approvals)
                if pending.remaining(results) is not None:
                    raise ValueError("Deferred requests are incomplete.")
            except (ValueError, KeyError):
                return {"resolved": True, "resumed": False, "session_id": session_id,
                        "note": "Resolve the other current questions or approvals before continuing."}
        resumed = await self.database.resume(
            actor, session_id, turn_id, "native-credentials:" + turn_id,
            TypeAdapter(DeferredToolResults).dump_python(results, mode="json"),
        )
        async with self.database.sessions.begin() as db:
            for row in await db.scalars(select(AccessRequest).where(AccessRequest.turn_id == turn_id)):
                row.resolution_turn_id = resumed.id
        return {"resolved": True, "resumed": True, "session_id": session_id, "turn_id": resumed.id}
