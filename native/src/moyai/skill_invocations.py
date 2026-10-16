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

from .contracts import RunScope
from .models import User
from .product_identity import ProductGrant
from .settings_models import SettingSkill
from .skill_mentions import display_text, marker, mentions

if TYPE_CHECKING:
    from .runtime import Runtime

TOKEN = re.compile(r"/((?:(?:personal|org|shared|bundled):)?[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=\s|$)")
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
        catalog = self.runtime.product_skill_catalog
        if catalog is not None:
            grant = getattr(user, "product_grant", None)
            if grant is None:
                raise HTTPException(403, "Refresh your sign-in before selecting a skill.")
            return await catalog.listing(grant, shared=shared, offset=offset, limit=limit)
        rows = await self.native_rows(user.id, shared)
        return {
            "skills": [{key: row[key] for key in ("id", "name", "description", "scope", "reference", "icon")}
                       for row in rows[offset:offset + limit]],
            "has_more": len(rows) > offset + limit,
            "next_offset": offset + limit if len(rows) > offset + limit else None,
        }

    async def resolve(
        self, prompt: str, *, actor_id: str, visibility: str, surface: str,
        grant: ProductGrant | None,
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
            if catalog is not None:
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
        selected, _ = await self.resolve(
            prompt, actor_id=user.id, visibility=session.visibility, surface=session.surface,
            grant=getattr(user, "product_grant", None),
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
        selected, remaining = await self.resolve(
            prompt, actor_id=scope.actor_id, visibility=scope.visibility, surface=scope.surface,
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
