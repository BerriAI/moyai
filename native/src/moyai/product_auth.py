"""Product sign-in and canonical user roles around the new execution core."""

from __future__ import annotations

import asyncio
import hmac
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .models import User

if TYPE_CHECKING:
    from .runtime import Runtime


class RoleEdit(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "member"]
    revision: int = Field(ge=0)


async def password_login(runtime: Runtime, password: str, request: Request, response: Response):
    product = runtime.product
    assert product is not None
    settings, security = product.settings, product.security
    security.check_origin(request)
    if settings.cloudflare_access_login or not settings.password_login_enabled:
        raise HTTPException(403, "Use your organization sign-in.")
    await runtime.database.limit_login(security.client_key(request), "product-password")
    if settings.workspace_password and hmac.compare_digest(password, settings.workspace_password):
        role = "admin"
    elif settings.workspace_member_password and hmac.compare_digest(password, settings.workspace_member_password):
        role = "member"
    else:
        raise HTTPException(401, "Incorrect workspace password.")
    # Only a verified existing authentication path creates/refreshes canonical
    # account rows. Native user projection never enrolls product accounts.
    await asyncio.to_thread(product.store.identity, {"method": "password", "role": role})
    sid = security.new_session(response, role)
    return {"csrf": security.csrf(sid), "authenticated": True, "role": role}


def routes(runtime: Runtime, actor) -> APIRouter:
    product = runtime.product
    assert product is not None
    router = APIRouter()
    router.include_router(product.google.routes())

    @router.get("/api/settings/users")
    async def users(user: User = Depends(actor)):
        if user.role != "admin":
            raise HTTPException(403, "An organization administrator must perform this action.")
        directory = await asyncio.to_thread(product.roles.directory)
        return {
            **directory,
            "users": [{
                **row, "id": row["email"], "username": row["email"],
                "display_name": row["name"] or row["email"], "enabled": True,
            } for row in directory["users"]],
            "source": "product", "capabilities": {
                "read": True, "create": False, "update": False, "delete": False, "roles": True,
            },
            "note": "Organization sign-in manages accounts. Roles are shared with existing Moyai.",
        }

    @router.put("/api/settings/users/role")
    async def role(body: RoleEdit, request: Request, user: User = Depends(actor)):
        if user.role != "admin":
            raise HTTPException(403, "An organization administrator must perform this action.")
        from app.user_roles import RoleChange
        # Reuse the product's optimistic revision, last-admin protection, audit,
        # and writer lock. The handler rechecks the actor inside that transaction.
        info = product.security.session_info(request)
        result = await asyncio.to_thread(
            product.roles.change, RoleChange(**body.model_dump()), info,
        )
        return result

    @router.post("/api/settings/users")
    @router.patch("/api/settings/users/{identity}")
    @router.delete("/api/settings/users/{identity}")
    async def native_account_mutation(user: User = Depends(actor)):
        raise HTTPException(409, "Manage this account through organization sign-in and product roles.")

    return router
