"""Bounded, relevant learned procedures for the current authorized repository."""

from typing import ClassVar

from app.memory import search_terms
from sqlalchemy import select

from ..contracts import RunScope
from ..message_sources import memory_text, public_message
from ..models import Message
from ..product_skill_learning import SkillLearningService
from ..product_slack_models import ProductSlackBinding, SlackTurnSource
from ..slack_audience import audience_reads
from .base import ContextSource


class LearnedSkills(ContextSource):
    name: ClassVar[str] = "learned_skills"

    def __init__(self, service: SkillLearningService):
        self.service = service

    async def _slack_audience(self, scope: RunScope) -> tuple[str, str, str, str] | None:
        """Organization procedures may enter a verified internal channel only."""
        service = self.service
        slack = service.runtime.product_slack
        if (
            slack is None or scope.product_grant is None or scope.surface != "slack"
            or scope.authority.surface != "slack" or scope.visibility != "shared"
            or scope.delegation_depth or scope.delegation_generated or scope.swarm_id
        ):
            return None
        principal = await service.identity.require_current(scope.product_grant)
        if principal.kind != "slack" or principal.id != scope.actor_id:
            return None
        async with service.database.sessions() as db:
            source = await db.get(SlackTurnSource, scope.turn_id)
            binding = await db.get(ProductSlackBinding, scope.session_id)
            if (
                source is None or binding is None or binding.paused
                or source.channel_id.startswith("D")
                or scope.actor_id != f"slack:{source.team_id}:{source.user_id}"
                or (source.team_id, source.channel_id, source.thread_ts)
                != (binding.team_id, binding.channel_id, binding.thread_ts)
            ):
                return None
            installed = await slack.installation_in(db)
            if (installed["team_id"], installed["authority"]) != (
                binding.team_id, binding.connection_authority,
            ):
                return None
            audience = (binding.team_id, binding.channel_id, binding.thread_ts, binding.connection_authority)
        # Provider I/O never owns a database transaction. The existing audience
        # cache bounds Slack-only changes to 60 seconds and rejects stale failures.
        info = await audience_reads(slack).read(slack, installed, "conversations.info", audience[1])
        channel = info.get("channel", {})
        if (
            info.get("ok") is not True or channel.get("id") != audience[1]
            or channel.get("is_ext_shared") is not False or channel.get("is_channel") is not True
        ):
            return None
        return audience

    async def render(self, scope: RunScope) -> str:
        service = self.service
        if (
            not service.settings.skill_learning_enabled or scope.product_grant is None
            or scope.surface not in {"web", "slack"}
        ):
            return ""
        await service.database.authorize_scope(scope)
        principal = await service.identity.require_current(scope.product_grant)
        audience = await self._slack_audience(scope) if scope.surface == "slack" else None
        if scope.surface == "slack" and audience is None:
            return ""
        repository = await service._repository(scope)
        async with service.database.sessions.begin() as db:
            await service._lock(db)
            messages = list(await db.scalars(select(Message).where(
                Message.turn_id == scope.turn_id, Message.author_id == scope.actor_id, Message.role == "user",
            )))
            for message in messages:
                _, transport = await public_message(service.database, db, message)
                if scope.surface == "slack":
                    if not transport or transport.get("kind") != "slack":
                        return ""
                elif (transport and transport.get("kind") != "web") or (message.author_id or "").startswith("slack:"):
                    return ""
            terms = search_terms(" ".join([await memory_text(service.database, db, item) for item in messages]))
            keys = ["organization"]
            if scope.surface == "web" and scope.visibility == "private" and principal.individual:
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
        if audience is not None and await self._slack_audience(scope) != audience:
            return ""
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
