"""Authenticated product entrypoints for native swarm missions."""

from fastapi import APIRouter, Depends, Query

from .models import User
from .runtime import Runtime
from .swarm_contracts import NewSwarm, SwarmControl


def routes(runtime: Runtime, actor) -> APIRouter:
    from .api import session_view, turn_view
    router = APIRouter()
    service = runtime.database.swarms

    @router.post("/api/swarms")
    async def start(body: NewSwarm, user: User = Depends(actor)):
        session, turn = await service.start(user, body)
        return {"session": {**session_view(session), "swarm_id": session.id}, "turn": turn_view(turn)}

    @router.get("/api/sessions/{session_id}/swarm")
    async def board(
        session_id: str, after: int | None = Query(default=None, ge=0, le=2_147_483_647),
        user: User = Depends(actor),
    ):
        return await service.view(user, session_id, after=after)

    @router.post("/api/swarms/{mission_id}/control")
    async def control(mission_id: str, body: SwarmControl, user: User = Depends(actor)):
        await service.control(user, mission_id, body)
        return await service.view(user, mission_id)

    return router
