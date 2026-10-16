"""Use Moyai's existing verified Access enrollment and browser-session policy."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from .product_resources import ProductResources


async def access_session(product: ProductResources, request: Request) -> bytes | None:
    if not product.settings.cloudflare_access_login:
        return None
    from app.cloudflare_access import AccessUnavailable
    from jwt import PyJWTError

    try:
        identity = await product.identity.access.check(
            "/api/product/identity", "GET", request.headers.get("cf-access-jwt-assertion", ""),
        )
        if identity is None:
            raise HTTPException(401, "A valid organization sign-in is required.")
        return await asyncio.to_thread(product.security.access_session, request, identity)
    except AccessUnavailable:
        raise HTTPException(503, "Organization sign-in verification is temporarily unavailable.") from None
    except (PyJWTError, ValueError, TypeError):
        raise HTTPException(401, "A valid organization sign-in is required.") from None
