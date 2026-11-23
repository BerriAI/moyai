"""Explicit skill references, resolved on the server at admission and execution.

The saved message retains references, not private instruction bodies. Expansion
happens inside the existing preparation activity and encrypted Temporal payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import or_, select

from .contracts import ExecutionContext, RunScope
from .models import MessageSource, Turn, User
from .preferences.settings_models import SettingSkill
from .product_identity import ProductGrant
from .skill_mentions import display_text, marker, mentions

if TYPE_CHECKING:
    from .runtime import Runtime

TOKEN = re.compile(r"/((?:(?:personal|org|shared|bundled|gateway-[0-9a-f-]{36}):)?[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=\s|$)")
MAX_SKILLS = 8
MAX_CONTEXT = 500_000


@dataclass(frozen=True)
class SlashReference:
    start: int
    end: int
    reference: str


SLASH = re.compile(r"/((?:(?:personal|org|shared|bundled|gateway-[0-9a-f-]{36}):)?[A-Za-z0-9][A-Za-z0-9_-]{0,99})(?=$|[\s,!?;:)]|\.(?=\s|$))")
INTENT = re.compile(r"\b(?:use|invoke|run|apply|load)\s+(?:(?:the\s+)?skill\s+)?$", re.IGNORECASE)
NEGATED_INTENT = re.compile(
    r"\b(?:don't|don’t|do\s+not|never|not)\s+(?:use|invoke|run|apply|load)\s+(?:(?:the\s+)?skill\s+)?$",
    re.IGNORECASE,
)


def _literal_end(prompt: str, start: int, opening: str, closing: str, *, nested: bool = False) -> int:
    """Skip an opaque quoted or linked region, including escaped delimiters."""
    index, depth = start + 1, 1
    while index < len(prompt):
        if prompt[index] == "\\":
            index += 2
            continue
        if nested and prompt[index] == opening:
            depth += 1
        elif prompt[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(prompt)


def slack_references(prompt: str) -> list[SlashReference]:
    """Recognize direct requests, never quoted/code/link/path examples.

    Bare names are explicit at the start, or after an invocation verb. This
    intentionally leaves discussions such as "explain /team" as ordinary text.
    Slack links and Markdown links are opaque, including their display labels.
    """
    result: list[SlashReference] = []
    index, leading, previous_end = 0, True, 0
    while index < len(prompt):
        if index == 0 or prompt[index - 1] == "\n":
            newline = prompt.find("\n", index)
            line = prompt[index:newline if newline >= 0 else len(prompt)]
            if line.lstrip().startswith((">>>", "&gt;&gt;&gt;")):
                break
            if line.startswith(("    ", "\t")) or line.lstrip().startswith((">", "&gt;")):
                index += len(line) + 1
                leading = False
                continue
        char = prompt[index]
        if char == "\\":
            index += 2
            leading = False
            continue
        if char in "`~" and (char == "`" or prompt.startswith("~~~", index)):
            end = index + 1
            while end < len(prompt) and prompt[end] == char:
                end += 1
            close = prompt.find(prompt[index:end], end)
            index = len(prompt) if close < 0 else close + end - index
            leading = False
            continue
        if char in "\"“‘" or (char == "'" and (index == 0 or not prompt[index - 1].isalnum())):
            index = _literal_end(prompt, index, char, {"“": "”", "‘": "’"}.get(char, char))
            leading = False
            continue
        if char in "<[":
            index = _literal_end(prompt, index, char, ">" if char == "<" else "]", nested=char == "[")
            if char == "[" and prompt[index:index + 1] == "(":
                index = _literal_end(prompt, index, "(", ")", nested=True)
            leading = False
            continue
        match = SLASH.match(prompt, index)
        if match and (index == 0 or prompt[index - 1].isspace()):
            prefix = prompt[previous_end:index]
            if leading or (INTENT.search(prefix) and not NEGATED_INTENT.search(prefix)) or (
                result and re.fullmatch(r"\s*(?:,\s*)?(?:and\s+|then\s+)?", prefix)
            ):
                result.append(SlashReference(index, match.end(), match[1]))
                if len(result) > MAX_SKILLS:
                    raise HTTPException(422, f"Select at most {MAX_SKILLS} skills per message.")
                previous_end = match.end()
                index = match.end()
                continue
        if not char.isspace():
            leading = False
        index += 1
    return result


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
        from .preferences.settings_api import bundled_skills

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
        selected_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        names, remaining = invocation_names(prompt) if selected_names is None else (selected_names, prompt)
        if len(names) > MAX_SKILLS:
            raise HTTPException(422, f"Select at most {MAX_SKILLS} skills per message.")
        if not names:
            return [], prompt
        if surface not in {"web", "slack"}:
            raise HTTPException(403, "Invoke skills in an authenticated web or Slack conversation.")
        if surface == "slack":
            if visibility != "shared" or grant is None or self.runtime.product_identity is None:
                raise HTTPException(403, "Slack skills require a verified requester in a shared conversation.")
            principal = await self.runtime.product_identity.require_current(grant)
            if principal.id != actor_id or principal.kind != "slack":
                raise HTTPException(403, "The skill requester does not match the verified Slack identity.")
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
                try:
                    row = await catalog.invocation(grant, name, shared=shared)
                except HTTPException as error:
                    if surface == "slack" and error.status_code == 422:
                        raise HTTPException(
                            422, f"Cannot invoke /{name}. Ask me to list skills, then use an available "
                            "scoped name such as /org:team.",
                        ) from None
                    raise
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

    async def normalize_slack(self, prompt: str, user: User, session: Any) -> str:
        """Resolve only the signed message's authored text, before adding thread/files."""
        # Slack has no trusted picker. Copied internal marker links are literals;
        # only this admission method may author executable markers for Slack.
        for mention in reversed(mentions(prompt)):
            prompt = prompt[:mention.start] + "\\" + prompt[mention.start:]
        refs = slack_references(prompt)
        if not refs:
            return prompt
        context = ExecutionContext(session.visibility, session.surface)
        if getattr(session, "id", None) is not None:
            async with self.runtime.database.sessions() as db:
                context = await self.runtime.database.execution_context(db, session)
        selected, _ = await self.resolve(
            prompt, actor_id=user.id, visibility=context.visibility, surface=context.surface,
            grant=getattr(user, "product_grant", None), load_gateway=False,
            selected_names=[ref.reference for ref in refs],
        )
        for ref in reversed(refs):
            row = next(row for row in selected if (
                row["reference"] if ":" in ref.reference else row["name"]
            ) == ref.reference)
            prompt = prompt[:ref.start] + marker(row["reference"], row.get("icon")) + prompt[ref.end:]
        return prompt

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

    async def expand(self, prompt: str, scope: RunScope, *, input_turn_id: str | None = None) -> str:
        if not has_invocations(prompt):
            return prompt
        selected_names = None
        async with self.runtime.database.sessions() as db:
            source = await db.get(MessageSource, f"user:{input_turn_id or scope.turn_id}")
            if source is not None and source.kind == "slack":
                turn = await db.get(Turn, input_turn_id or scope.turn_id)
                if turn is None or turn.session_id != scope.session_id or turn.actor_id != scope.actor_id:
                    raise HTTPException(403, "The Slack skill selection does not belong to this requester.")
                # Never interpret file excerpts or fetched thread text as the
                # sender's selection, including in a DM linked to a web session.
                # Keep the admission snapshot when Slack edits the transcript
                # after execution starts. Queued edits update it atomically.
                selected_names = json.loads(self.runtime.security.unseal(source.data)).get("skill_references", [])
                if not selected_names:
                    return prompt
        await self.runtime.database.authorize_scope(scope)
        gateway_ids = gateway_connection_ids(prompt) if selected_names is None else [
            name.removeprefix("gateway-").split(":", 1)[0]
            for name in selected_names if name.startswith("gateway-")
        ]
        if set(gateway_ids) - {grant.id for grant in scope.grants if grant.kind == "litellm"}:
            raise HTTPException(403, "The selected skill requires its admitted gateway connection.")
        selected, remaining = await self.resolve(
            prompt, actor_id=scope.actor_id, visibility=scope.authority.visibility, surface=scope.authority.surface,
            grant=scope.product_grant, selected_names=selected_names,
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
