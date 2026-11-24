from fastapi import APIRouter, Depends

from .dot_contracts import DotView
from .models import User


def routes(runtime, actor) -> APIRouter:
    router = APIRouter()

    @router.get("/api/dot", operation_id="getPersonalDot")
    async def read(user: User = Depends(actor)) -> DotView:
        return DotView(
            session_id=await runtime.database.dots.read(user),
            attention_available=runtime.product_automations is not None,
        )

    @router.post("/api/dot", operation_id="openPersonalDot")
    async def open_dot(user: User = Depends(actor)) -> DotView:
        return DotView(
            session_id=await runtime.database.dots.open(user),
            attention_available=runtime.product_automations is not None,
        )

    return router
