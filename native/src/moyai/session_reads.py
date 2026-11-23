"""Authorized native session projections, including access rechecks after I/O.

HTTP handlers supply the authenticated user and request; this module owns the
read ordering and sensitive projections. Keep operation-specific checks here:
failure details are requester-only, delegated actions use assignment scope, and
waiting sessions load checkpoints only when pending questions need them.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from .api_views import (
    SavedActivity,
    SessionDetail,
    TurnFailureDetails,
    TurnWorkProgress,
    WorkActionDetail,
    session_view,
    turn_view,
)
from .failed_action_attempts import failed_action_attempt
from .failure_diagnostics import failure_details
from .models import Effect, Event, Message, Session, SessionPreference, SessionRead, Turn, User
from .product_requester import can_answer

if TYPE_CHECKING:
    from .runtime import Runtime

SAVED_ACTIVITY = TypeAdapter(list[SavedActivity])


class SessionReads:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def _require_current(self, session_id: str, request: Request, user: User) -> None:
        """Release a projection only while the original reader still has access.

        Preserve native epoch → product grant → session audience ordering. This
        runs after the read's transaction closes, so a recheck can use one-slot
        pools and must not be folded into the earlier authorization snapshot.
        """
        runtime = self.runtime
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        if runtime.product_identity is not None:
            await runtime.product_identity.require_request_current(request)
        await runtime.database.session(user, session_id)

    async def detail(
        self, session_id: str, request: Request, user: User, *, resolve: bool = False,
        include_activity: bool = True, include_messages: bool = False,
    ) -> SessionDetail:
        runtime = self.runtime
        reference = None
        if resolve and runtime.product_identity is not None:
            from .product_routes import ProductReadService, Reader
            # actor has just verified the product principal. The read adapter
            # still fences source/native ACLs and canonical continuation aliases.
            reference = (await ProductReadService(runtime).detail(
                Reader(user, request.state.product_principal), session_id, "native",
            ))["session"]
        await runtime.database.prefetch_session_audiences(user, [session_id])
        async with runtime.database.sessions() as db:
            row = await runtime.database.allowed_session(db, session_id, user, metadata_only=True)
            # Only a waiting conversation needs its private checkpoint to render
            # pending questions. Ordinary reads must not transfer megabytes of it.
            if row.status == "waiting":
                # Keep the state and active turn aligned with this checkpoint if
                # execution advanced while the source audience was checked.
                await db.refresh(row, ["status", "active_turn", "conversation"])
        turn_columns = load_only(
            Turn.id, Turn.session_id, Turn.actor_id, Turn.model, Turn.status, Turn.mode,
            Turn.parent_turn, Turn.enqueue_id, Turn.connections, Turn.accepted_at, Turn.usage,
            Turn.error, Turn.created_at, Turn.finished_at,
        )
        async with runtime.database.sessions() as db:
            turns = list((await db.scalars(select(Turn).where(
                Turn.session_id == session_id
            ).options(turn_columns).order_by(Turn.created_at.desc()).limit(100))).all())
            if row.active_turn and not any(turn.id == row.active_turn for turn in turns):
                active_turn = await db.get(Turn, row.active_turn, options=[turn_columns])
                if active_turn is not None:
                    turns.append(active_turn)
            guidance = dict((await db.execute(select(Event.turn_id, Event.created_at).where(
                Event.turn_id.in_([turn.id for turn in turns]), Event.kind == "steering_admitted",
            ).order_by(Event.id))).all())
            # Resolve each relationship once for this projection/transaction.
            # A linked Slack author may own many turns; repeating its admission
            # and identity reads per turn adds hundreds of database round trips.
            owners = {owner: await runtime.database.same_person(db, user, owner)
                      for owner in {row.owner_id, *(turn.actor_id for turn in turns)}}
            result: dict[str, Any] = {
                "session": {**session_view(row), "can_manage": owners[row.owner_id]}, "turns": [
                    {**turn_view(turn), "guidance_at": guidance.get(turn.id),
                     "is_self": owners[turn.actor_id]} for turn in turns
                ],
            }
            if include_messages:
                from .models import MessageSource
                version = (await db.execute(select(
                    func.coalesce(func.max(Message.id), 0), func.count(MessageSource.message_identity),
                    func.coalesce(func.sum(MessageSource.revision), 0),
                ).outerjoin(MessageSource, MessageSource.message_identity == Message.identity).where(
                    Message.session_id == session_id,
                ))).one()
                authors = (await db.execute(select(User.id, User.display_name, User.auth_version).join(
                    Message, Message.author_id == User.id,
                ).where(Message.session_id == session_id).distinct().order_by(User.id))).all()
                identities = [(author.id, author.display_name, author.auth_version,
                               await runtime.database.same_person(db, user, author.id)) for author in authors]
                identity_version = hashlib.sha256(json.dumps(identities).encode()).hexdigest()
                result["transcript_version"] = ":".join(str(part) for part in (*version, identity_version))
            if row.surface == "slack":
                from .slack_delivery_status import status as delivery_status
                result["slack_delivery"] = await delivery_status(runtime.database, db, session_id)
            swarm_member = await runtime.database.swarms.member(db, session_id)
            from .goal_models import Goal
            goal = await db.get(Goal, session_id)
            result["goal"] = runtime.database.goals.view(goal)
            if goal is not None and result["goal"] is not None:
                result["goal"]["elapsed_seconds"] = await runtime.database.goals.elapsed_seconds(db, goal)
            if swarm_member is not None:
                result["session"]["swarm_id"] = swarm_member.mission_id
            from .delegation_models import DelegatedSession
            delegation = await db.get(DelegatedSession, session_id)
            if delegation is not None:
                try:
                    await runtime.database.allowed_session(db, delegation.parent_session_id, user, metadata_only=True)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    result["session"]["parent_session_id"] = delegation.parent_session_id
            if reference is not None:
                result["reference"] = reference
            preference = await db.get(SessionPreference, (user.id, session_id))
            result["session"]["pinned"] = bool(preference and preference.pinned)
            result["session"]["folder"] = preference.folder if preference else ""
            read = await db.get(SessionRead, (user.id, session_id))
            result["session"]["unread"] = bool(read and read.unread)
            active_turn = next((turn for turn in turns if turn.id == row.active_turn), None)
            unsettled = list((await db.scalars(select(Effect).where(
                Effect.session_id == session_id, Effect.state == "pending",
                ~failed_action_attempt(),
            ))).all())
            result["has_unsettled_effects"] = bool(unsettled)
            result["effects"] = [{
                "id": effect.id, "turn_id": effect.turn_id, "tool_name": effect.tool_name,
                "arguments": json.loads(runtime.security.unseal(effect.arguments)),
            } for effect in unsettled if await runtime.database.same_person(db, user, effect.actor_id)]
        if include_activity and turns and turns[0].status != "running":
            from .progress import turn_activity
            async with runtime.database.sessions() as db:
                latest = await db.get(Turn, turns[0].id, options=[load_only(Turn.id, Turn.preparation)])
            if latest is not None:
                result["activity"] = await turn_activity(runtime, latest)
            await self._require_current(session_id, request, user)
        if row.status == "waiting" and row.conversation and active_turn is not None:
            # Requester-link checks take their own transaction. Do not hold a
            # native connection while awaiting them (including one-slot pools).
            result["can_answer"] = await can_answer(runtime, user, active_turn)
            if result["can_answer"]:
                conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(row.conversation))
                result["pending"] = TypeAdapter(Any).dump_python(conversation.deferred_tool_requests, mode="json")
                from .questions import can_reply_with_text
                result["can_reply_with_text"] = can_reply_with_text(conversation.deferred_tool_requests)
                async with runtime.database.sessions() as db:
                    deadline = await db.scalar(select(Event).where(
                        Event.identity == f"optional-question:{active_turn.id}",
                    ))
                if deadline is not None:
                    result["question_deadline"] = json.loads(runtime.security.unseal(deadline.content))["deadline"]
            elif runtime.product_secrets is not None and user.role == "admin":
                # Organization access assistance does not confer permission to
                # answer another requester's questions or approve their tools.
                requests = await runtime.product_secrets.execution.pending(request.state.product_principal, session_id)
                result["credential_requests"] = [
                    {"request_id": item["request_id"]} for item in requests["requests"]
                ]
        if include_messages:
            result["messages"] = await runtime.database.messages(user, session_id)
            mention_names = [(message['id'], message.get('slack_mentions', {})) for message in result['messages']]
            result['transcript_version'] += ':' + hashlib.sha256(
                json.dumps(mention_names, sort_keys=True).encode(),
            ).hexdigest()
        return SessionDetail.model_validate(result)

    async def progress(
        self, session_id: str, turn_id: str, request: Request, user: User, *,
        include_activity: bool = False,
    ) -> TurnWorkProgress:
        runtime = self.runtime
        from .progress import turn_progress

        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        result = (
            await runtime.work_progress.read(turn) if include_activity
            else {"messages": await turn_progress(runtime, turn)}
        )
        await self._require_current(session_id, request, user)
        return TurnWorkProgress.model_validate(result)

    async def action(
        self, session_id: str, turn_id: str, action_id: str, request: Request, user: User, *,
        agent_id: str | None = None,
    ) -> WorkActionDetail:
        runtime = self.runtime
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        result, source_session = await runtime.work_progress.action(turn, action_id, agent_id)
        await self._require_current(session_id, request, user)
        if source_session != session_id:
            # action() resolves only generated turns in this parent's assignment,
            # not later private follow-ups in the delegated conversation.
            async with runtime.database.sessions() as db:
                source = await db.get(Session, source_session)
                if source is None or source.deleted:
                    raise HTTPException(404, "Action not found.")
        return WorkActionDetail.model_validate(result)

    async def failure(
        self, session_id: str, turn_id: str, request: Request, user: User,
    ) -> TurnFailureDetails:
        runtime = self.runtime
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id or not turn.error:
                raise HTTPException(404, "Failure not found.")
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Failure details are visible only to the requester.")
        details = await failure_details(runtime, turn)
        await self._require_current(session_id, request, user)
        return TurnFailureDetails.model_validate(details)

    async def activity(self, session_id: str, request: Request, user: User) -> list[SavedActivity]:
        runtime = self.runtime
        from .progress import turn_activity

        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.scalar(select(Turn).where(
                Turn.session_id == session_id,
            ).options(load_only(Turn.id, Turn.status, Turn.preparation)).order_by(Turn.created_at.desc()).limit(1))
        result = await turn_activity(runtime, turn) if turn is not None and turn.status != "running" else []
        await self._require_current(session_id, request, user)
        return SAVED_ACTIVITY.validate_python(result)
