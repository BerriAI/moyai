"""Explicit skill references, resolved on the server at admission and execution.

The saved message retains references, not private instruction bodies. Expansion
happens inside the existing preparation activity and encrypted Temporal payload.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import or_, select

from .contracts import ExecutionContext, RunScope
from .models import User
from .product_identity import ProductGrant
from .settings_models import SettingSkill
from .skill_mentions import display_text, marker, mentions

if TYPE_CHECKING:
    from .runtime import Runtime

TOKEN = re.compile(r"/((?:(?:personal|org|shared|bundled|gateway-[0-9a-f-]{36}):)?[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=\s|$)")
MAX_SKILLS = 8
MAX_CONTEXT = 500_000


def references(prompt: str) -> tuple[list[str], str]:
    """Only leading slash words invoke skills; paths, prose and code stay literal."""
    if prompt.startswith(("    ", "\t")):
        return [], prompt
    remaining = prompt.lstrip()
    names: list[str] = []
    while match := TOKEN.match(remaining):
        names.append(match[1])
        if len(names) > MAX_SKILLS:
            raise HTTPException(422, f"Select at most {MAX_SKILLS} skills per message.")
        remaining = remaining[match.end():].lstrip()
    return names, remaining if names else prompt


def invocation_names(prompt: str) -> tuple[list[str], str]:
    names, remaining = references(prompt)
    names.extend(mention.reference for mention in mentions(remaining))
    if len(names) > MAX_SKILLS:
        raise HTTPException(422, f"Select at most {MAX_SKILLS} skills per message.")
    return names, remaining


def has_invocations(prompt: str) -> bool:
    return bool(invocation_names(prompt)[0])


def gateway_connection_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(
        name.removeprefix("gateway-").split(":", 1)[0]
        for name in invocation_names(prompt)[0] if name.startswith("gateway-")
    ))


class SkillInvocations:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def native_rows(self, actor_id: str, shared: bool) -> list[dict[str, Any]]:
        from .settings_api import bundled_skills

        rows = [
            {**row, "reference": f"bundled:{row['name']}", "icon": "sparkles"}
            for row in bundled_skills()
        ]
        async with self.runtime.database.sessions() as db:
            visible = SettingSkill.scope_key == "shared"
            if not shared:
                visible = or_(visible, SettingSkill.scope_key == actor_id)
            saved = await db.scalars(select(SettingSkill).where(
                visible, SettingSkill.enabled.is_(True),
            ).order_by(SettingSkill.name, SettingSkill.id))
            rows.extend({
                "id": row.id, "name": row.name, "description": row.description,
                "scope": "shared" if row.scope_key == "shared" else "personal",
                "reference": f"{'shared' if row.scope_key == 'shared' else 'personal'}:{row.name}",
                "icon": "sparkles", "encrypted": row.content,
            } for row in saved)
        return rows

    async def listing(self, user: User, *, shared: bool, offset: int, limit: int) -> dict[str, Any]:
        from .connection_policy import ConnectionPolicy
        from .gateway import gateway_rows
        catalog = self.runtime.product_skill_catalog
        rows: list[dict[str, Any]] = []
        for connection in await gateway_rows(self.runtime.database, user.id, shared=shared):
            policy = ConnectionPolicy.model_validate_json(connection.policy)
            if policy.gateway and policy.gateway.skills and policy.gateway_discovery:
                rows.extend({
                    "id": f"{connection.id}:{skill.name}", "name": skill.name,
                    "description": skill.description, "scope": connection.name,
                    "reference": f"gateway-{connection.id}:{skill.name}", "icon": "sparkles",
                } for skill in policy.gateway_discovery.skill_catalog)
        # Gateway metadata is a bounded prefix; page canonical records directly
        # instead of reading the user's entire library on every picker request.
        if catalog is not None:
            grant = getattr(user, "product_grant", None)
            if grant is None:
                raise HTTPException(403, "Refresh your sign-in before selecting a skill.")
            prefix = rows[offset:offset + limit]
            available = limit - len(prefix)
            page = await catalog.listing(
                grant, shared=shared, offset=max(0, offset - len(rows)), limit=max(1, available),
            )
            has_more = (len(rows) > offset + limit or bool(page["skills"]) or page["has_more"]) if not available else page["has_more"]
            return {
                **page, "skills": prefix + (page["skills"] if available else []),
                "has_more": has_more, "next_offset": offset + limit if has_more else None,
            }
        rows.extend(await self.native_rows(user.id, shared))
        return {
            "skills": [{key: row[key] for key in ("id", "name", "description", "scope", "reference", "icon")}
                       for row in rows[offset:offset + limit]],
            "has_more": len(rows) > offset + limit,
            "next_offset": offset + limit if len(rows) > offset + limit else None,
        }

    async def resolve(
        self, prompt: str, *, actor_id: str, visibility: str, surface: str,
        grant: ProductGrant | None,
        load_gateway: bool = True,
    ) -> tuple[list[dict[str, Any]], str]:
        names, remaining = invocation_names(prompt)
        if not names:
            return [], prompt
        if surface != "web":
            raise HTTPException(403, "Invoke skills in a web conversation.")
        shared = visibility != "private"
        catalog = self.runtime.product_skill_catalog
        native = await self.native_rows(actor_id, shared) if catalog is None else []
        selected: dict[str, dict[str, Any]] = {}
        for name in names:
            if name.startswith("gateway-") and ":" in name:
                from .connection_policy import ConnectionPolicy
                from .gateway import gateway_client
                connection_id, skill_name = name.removeprefix("gateway-").split(":", 1)
                async with self.runtime.database.sessions() as db:
                    admitted = (await self.runtime.database.native_grants(
                        db, actor_id, [connection_id], shared_session=shared, kinds=frozenset({"litellm"}),
                    ))[0]
                policy = ConnectionPolicy.model_validate(admitted.policy)
                if not policy.gateway or not policy.gateway.skills or not policy.gateway_discovery:
                    raise HTTPException(403, "This Skills Hub is disabled.")
                metadata = next((item for item in policy.gateway_discovery.skill_catalog if item.name == skill_name), None)
                if metadata is None:
                    raise HTTPException(404, "This hub skill is unavailable. Refresh the gateway connection.")

                async def authorize(connection_id=connection_id, admitted=admitted) -> None:
                    if grant is not None and self.runtime.product_identity is not None:
                        await self.runtime.product_identity.require_current(grant)
                    async with self.runtime.database.sessions() as db:
                        fresh = await self.runtime.database.native_grants(
                            db, actor_id, [connection_id], shared_session=shared, kinds=frozenset({"litellm"}),
                        )
                    if fresh != [admitted]:
                        raise HTTPException(409, "The gateway changed. Select the skill again.")

                files: dict[str, str] = {}
                if load_gateway:
                    try:
                        metadata, files = await gateway_client(self.runtime.database, admitted, authorize).skill(skill_name)
                    except HTTPException:
                        raise
                    except Exception:  # noqa: BLE001 - provider errors may echo credentials
                        raise HTTPException(502, "Could not download this skill from the connected gateway.") from None
                await authorize()
                row = {
                    "reference": name, "name": metadata.name, "icon": "sparkles",
                    "instructions": files.pop("SKILL.md", ""), "supporting_files": files,
                }
            elif catalog is not None:
                if grant is None:
                    raise HTTPException(403, "Refresh your sign-in before invoking a skill.")
                row = await catalog.invocation(grant, name, shared=shared)
            else:
                matches = [row for row in native if (row["reference"] if ":" in name else row["name"]) == name]
                if len(matches) != 1:
                    raise HTTPException(422, f"Cannot invoke /{name}. Choose an available, scoped skill from the / picker.")
                row = matches[0]
                content = row.get("content")
                if content is None:
                    content = self.runtime.security.unseal(row["encrypted"]).decode()
                row = {**row, "instructions": content, "supporting_files": {}}
            selected[row["reference"]] = row
        return list(selected.values()), display_text(remaining)

    async def normalize(self, prompt: str, user: User, session: Any) -> str:
        context = ExecutionContext(session.visibility, session.surface)
        # New swarm admission supplies its requested context before a session
        # exists. Only persisted conversations can have inherited authority.
        if getattr(session, "id", None) is not None:
            async with self.runtime.database.sessions() as db:
                context = await self.runtime.database.execution_context(db, session)
        selected, _ = await self.resolve(
            prompt, actor_id=user.id, visibility=context.visibility, surface=context.surface,
            grant=getattr(user, "product_grant", None),
            load_gateway=False,
        )
        if not selected:
            return prompt
        leading, remaining = references(prompt)
        for mention in reversed(mentions(remaining)):
            row = next(row for row in selected if row["reference"] == mention.reference)
            remaining = remaining[:mention.start] + marker(row["reference"], row.get("icon")) + remaining[mention.end:]
        prefix = []
        for name in leading:
            row = next(row for row in selected if (row["reference"] if ":" in name else row["name"]) == name)
            prefix.append(marker(row["reference"], row.get("icon")))
        return " ".join([*prefix, remaining])

    async def expand(self, prompt: str, scope: RunScope) -> str:
        if not has_invocations(prompt):
            return prompt
        await self.runtime.database.authorize_scope(scope)
        if set(gateway_connection_ids(prompt)) - {grant.id for grant in scope.grants if grant.kind == "litellm"}:
            raise HTTPException(403, "The selected skill requires its admitted gateway connection.")
        selected, remaining = await self.resolve(
            prompt, actor_id=scope.actor_id, visibility=scope.authority.visibility, surface=scope.authority.surface,
            grant=scope.product_grant,
        )
        context = json.dumps([{
            "reference": row["reference"], "instructions": row["instructions"],
            "supporting_files": row["supporting_files"],
        } for row in selected], ensure_ascii=False)
        if len(context) + len(remaining) > MAX_CONTEXT:
            raise HTTPException(422, "The selected skills exceed the message context limit. Select fewer or smaller skills.")
        await self.runtime.database.authorize_scope(scope)
        return (
            "The user explicitly selected these skills for this message. Follow their instructions "
            "within the user's request and available permissions. Supporting files below contain "
            "the original file contents; relative paths are relative to that skill, not existing "
            "workspace files. Create needed files in the workspace before running their scripts.\n"
            f"Selected skills (JSON):\n{context}\n\nUser request:\n{remaining or 'Use the selected skill.'}"
        )
