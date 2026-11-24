"""Authenticated private responsibility controls and inbox."""

from fastapi import APIRouter, Depends, HTTPException

from .models import User
from .responsibility_contracts import (
    NewResponsibility,
    ResponsibilityControl,
    ResponsibilityHistory,
    ResponsibilityInbox,
    ResponsibilityList,
    ResponsibilityOutput,
    ResponsibilityView,
)


class ResponsibilityReadResult(ResponsibilityOutput):
    read: bool


def routes(runtime, actor) -> APIRouter:
    router = APIRouter()
    service = runtime.database.responsibilities

    @router.get("/api/responsibilities", operation_id="listResponsibilities")
    async def listing(user: User = Depends(actor)) -> ResponsibilityList:
        return ResponsibilityList(items=await service.list(user))

    @router.post("/api/responsibilities", operation_id="createResponsibility")
    async def create(body: NewResponsibility, user: User = Depends(actor)) -> ResponsibilityView:
        return await service.create(user, body)

    @router.post("/api/responsibilities/{responsibility_id}/control", operation_id="controlResponsibility")
    async def control(
        responsibility_id: str, body: ResponsibilityControl, user: User = Depends(actor),
    ) -> ResponsibilityView:
        return await service.control(user, responsibility_id, body)

    @router.get("/api/responsibilities/{responsibility_id}/reviews", operation_id="listResponsibilityReviews")
    async def reviews(responsibility_id: str, user: User = Depends(actor)) -> ResponsibilityHistory:
        try:
            return ResponsibilityHistory(items=await service.history(user, responsibility_id))
        except PermissionError:
            raise HTTPException(404, "Responsibility not found.") from None

    @router.get("/api/responsibility-inbox", operation_id="listResponsibilityInbox")
    async def inbox(user: User = Depends(actor)) -> ResponsibilityInbox:
        return ResponsibilityInbox(items=await service.inbox(user))

    @router.post("/api/responsibility-inbox/{turn_id}/read", operation_id="markResponsibilityFindingRead")
    async def read(turn_id: str, user: User = Depends(actor)) -> ResponsibilityReadResult:
        await service.mark_read(user, turn_id)
        return ResponsibilityReadResult(read=True)

    return router
