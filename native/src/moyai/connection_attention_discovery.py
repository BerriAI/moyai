"""Discover declared read tools without holding a database transaction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from .attention_policy import declared_reads
from .connection_attention import native_grant
from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .mcp_remote import remote_mcp_toolset
from .models import Connection, User

if TYPE_CHECKING:
    from .runtime import Runtime


async def discover_reads(
    runtime: Runtime, grant: Grant, authorize: Callable[[], Awaitable[None]],
) -> list[str]:
    policy = ConnectionPolicy.model_validate(grant.policy)
    await authorize()
    declared = declared_reads(grant.kind, policy)
    if declared is not None:
        return sorted(name for name in declared if policy.allows(name))
    config = policy.mcp if grant.kind == "mcp" else None
    if (
        grant.kind == "litellm" and policy.gateway and policy.gateway.mcp
        and policy.gateway_discovery and policy.gateway_discovery.mcp.status == "ready"
    ):
        config = policy.gateway.endpoint_config("/mcp")
    if config is None:
        raise HTTPException(422, "This account has no supported attention read tools.")
    try:
        async with remote_mcp_toolset(
            config, runtime.security.unseal(grant.credential).decode(),
            runtime.settings.mcp_allowed_origins, identity=f"attention_check_{grant.id}", authorize=authorize,
        ) as tools:
            definitions = await tools.list_tools()
        names = sorted({
            tool.name for tool in definitions if policy.allows(tool.name)
            and tool.annotations is not None
            and tool.annotations.model_dump(by_alias=True).get("readOnlyHint") is True
            and not (grant.kind == "litellm" and tool.name.startswith("skills_"))
        })
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - provider errors can contain credentials
        await authorize()
        raise HTTPException(502, "Could not discover this account's read tools. Check its connection and try again.") from None
    await authorize()
    return names


async def prepare_policy(
    runtime: Runtime, request: Request, user: User, proposed: Grant, *, original: Grant | None = None,
) -> ConnectionPolicy:
    """An explicit first opt-in chooses current reads; later saves never add tools."""
    if runtime.product_automations is None:
        raise HTTPException(409, "Background attention is unavailable on this installation.")

    async def authorize() -> None:
        if runtime.product_identity is not None:
            principal = await runtime.product_identity.require_request_current(request)
            if not principal.individual:
                raise HTTPException(403, "Sign in with your individual work account.")
        fresh_user = await runtime.database.user(user.id)
        if not fresh_user.enabled or fresh_user.auth_version != user.auth_version:
            raise HTTPException(401, "Your account changed. Sign in again.")
        if original is not None:
            async with runtime.database.sessions() as db:
                current = await db.get(Connection, original.id, populate_existing=True)
                if current is None or current.owner_id != user.id or native_grant(current) != original:
                    raise HTTPException(409, "The connection changed during discovery. Refresh and try again.")

    available = set(await discover_reads(runtime, proposed, authorize))
    policy = ConnectionPolicy.model_validate(proposed.policy)
    previous = ConnectionPolicy.model_validate(original.policy) if original is not None else None
    selection = policy.attention_tools
    if selection is None and previous is not None:
        selection = previous.attention_tools
    if selection is None:
        selection = sorted(available)
    elif previous is not None and selection == previous.attention_tools:
        # Ordinary permission reductions or provider removals narrow the stored
        # subset. New provider tools never join an existing consent implicitly.
        selection = sorted(set(selection) & available)
    elif not set(selection) <= available:
        raise HTTPException(422, "Attention tools must be declared read-only by this account.")
    if not selection:
        raise HTTPException(422, "This account has no selected read tools. Save without proactive checks or retry discovery.")
    if len(set(selection)) > 200:
        raise HTTPException(422, "Choose at most 200 attention read tools.")
    policy.attention_tools = sorted(set(selection))
    return policy
