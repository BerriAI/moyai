from fastapi import APIRouter, Depends

from .dot_contracts import DotItemEdit, DotItemsView, DotItemView, DotView
from .dot_notifications import DotNotificationEdit, DotNotifications, DotNotificationSettings
from .models import User


def routes(runtime, actor) -> APIRouter:
    router = APIRouter()
    notifications = DotNotifications(runtime)

    @router.get("/api/dot/notifications", operation_id="getDotNotifications")
    async def notification_settings(user: User = Depends(actor)) -> DotNotificationSettings:
        return await notifications.read(user)

    @router.patch("/api/dot/notifications", operation_id="updateDotNotifications")
    async def edit_notifications(body: DotNotificationEdit, user: User = Depends(actor)) -> DotNotificationSettings:
        return await notifications.edit(user, body)

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

    @router.get("/api/dot/items", operation_id="listDotItems")
    async def items(user: User = Depends(actor)) -> DotItemsView:
        return await runtime.database.dots.discovery.list(user)

    @router.patch("/api/dot/items/{identity}", operation_id="editDotItem")
    async def edit(identity: str, body: DotItemEdit, user: User = Depends(actor)) -> DotItemView:
        return await runtime.database.dots.discovery.edit(user, identity, body)

    return router
