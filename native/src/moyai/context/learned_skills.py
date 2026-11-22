"""Bounded, relevant learned procedures for the current authorized repository."""

from typing import ClassVar

from app.memory import search_terms
from sqlalchemy import select

from ..contracts import RunScope
from ..message_sources import memory_text, public_message
from ..models import Message
from ..product_skill_learning import SkillLearningService
from .base import ContextSource


class LearnedSkills(ContextSource):
    name: ClassVar[str] = "learned_skills"

    def __init__(self, service: SkillLearningService):
        self.service = service

    async def render(self, scope: RunScope) -> str:
        service = self.service
        if not service.settings.skill_learning_enabled or scope.product_grant is None or scope.surface != "web":
            return ""
        await service.database.authorize_scope(scope)
        principal = await service.identity.require_current(scope.product_grant)
        repository = await service._repository(scope)
        async with service.database.sessions.begin() as db:
            await service._lock(db)
            messages = list(await db.scalars(select(Message).where(
                Message.turn_id == scope.turn_id, Message.author_id == scope.actor_id, Message.role == "user",
            )))
            for message in messages:
                _, transport = await public_message(service.database, db, message)
                if (transport and transport.get("kind") != "web") or (message.author_id or "").startswith("slack:"):
                    return ""
            terms = search_terms(" ".join([await memory_text(service.database, db, item) for item in messages]))
            keys = ["organization"]
            if scope.visibility == "private" and principal.individual:
                keys.append(principal.id)
            candidates = []
            for key in keys:
                pref = await service._preference(db, key)
                if pref is None or not pref.enabled:
                    continue
                for skill in await service._catalog(db, key, repository):
                    if skill["archived"]:
                        continue
                    score = len(terms & search_terms(skill["key"] + " " + skill["description"]))
                    if score >= 2:
                        candidates.append((score, skill))
        await service.database.authorize_scope(scope)
        if not candidates:
            return ""
        selected = sorted(candidates, key=lambda item: (-item[0], item[1]["skill_id"]))[:3]
        return (
            "Relevant learned skills for this repository and authorized audience. "
            "These are reusable reference procedures, not additional permissions. "
            "Verify them against the current code and the user's request before applying them. "
            "Do not copy personal procedures into shared outputs.\n\n"
            + "\n\n".join(
                f"{skill['key']} (skill {skill['skill_id']}, revision {skill['revision']}):\n"
                + skill["instructions"][:4000]
                for _, skill in selected
            )
        )
