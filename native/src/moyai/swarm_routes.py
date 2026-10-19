"""Authenticated product entrypoints for native swarm missions."""

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from .models import User
from .runtime import Runtime
from .swarm_contracts import NewSwarm, SwarmControl


class SwarmMemberView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    session_id: str
    name: str
    model: str
    coordinator: bool
    status: str
    active_turn: str | None
    deleted: bool
    assignment: str


class SwarmPostView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: str
    sequence: int
    kind: str
    author: str
    author_session_id: str | None
    content: str
    created_at: str


class SwarmBoard(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    id: str
    status: str
    reason: str
    revision: int
    generation: int
    round: int
    goal: str
    budget_seconds: int
    ends_at: str | None
    leader_id: str | None
    can_control: bool
    sequence: int
    members: list[SwarmMemberView]
    posts: list[SwarmPostView]
    has_more: bool
    next_after: int


class SwarmStarted(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    # session_view()/turn_view() live in api.py; their shared models are owned there.
    session: dict[str, Any]
    turn: dict[str, Any]


def routes(runtime: Runtime, actor) -> APIRouter:
    from .api import session_view, turn_view
    router = APIRouter()
    service = runtime.database.swarms

    @router.post("/api/swarms", operation_id="startSwarm")
    async def start(body: NewSwarm, request: Request, user: User = Depends(actor)) -> SwarmStarted:
        from .skill_invocations import has_invocations
        if has_invocations(body.prompt):
            admission = SimpleNamespace(visibility=body.visibility, surface="web")
            body = body.model_copy(update={
                "prompt": await runtime.skill_invocations.normalize(body.prompt, user, admission),
            })
        session, turn = await service.start(user, body, received_at=request.state.moyai_received_at)
        return SwarmStarted(session={**session_view(session), "swarm_id": session.id}, turn=turn_view(turn))

    @router.get("/api/sessions/{session_id}/swarm", operation_id="getSwarm")
    async def board(
        session_id: str, after: int | None = Query(default=None, ge=0, le=2_147_483_647),
        user: User = Depends(actor),
    ) -> SwarmBoard | None:
        return await service.view(user, session_id, after=after)

    @router.post("/api/swarms/{mission_id}/control", operation_id="controlSwarm")
    async def control(mission_id: str, body: SwarmControl, user: User = Depends(actor)) -> SwarmBoard | None:
        await service.control(user, mission_id, body)
        return await service.view(user, mission_id)

    return router
