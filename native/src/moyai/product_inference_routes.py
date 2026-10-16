"""Authenticated native model broker; Pydantic retains execution and streaming.

Existing deployment routing sends /broker/* to the broker role. Workers use
settings.public_url, exactly as the source runner does. The signed envelope is
bound to one materialized SDK request, original scope, and Temporal activity.
It is not a user login, run token, or permission to replay a paid request.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx2
from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import TypeAdapter, ValidationError
from starlette.responses import Response, StreamingResponse

from .contracts import RunScope
from .inference_usage import INFERENCE_ACTIVITY, InferenceActivity, InferenceUsageTransport
from .models import now
from .product_inference import InferenceAdmissionError, drain

if TYPE_CHECKING:
    from .inference_usage import UsageCapture
    from .product_inference import ProductInferenceService

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 64 * 1024
ROUTES = {"responses", "responses/compact", "chat/completions"}
SCOPE = TypeAdapter(RunScope)


def _signer(service: ProductInferenceService) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(service.settings.session_secret, salt="moyai-native-inference-v1")


def broker_request(
    service: ProductInferenceService, request: httpx2.Request, capture: UsageCapture,
) -> httpx2.Request:
    activity = capture.activity
    if activity.logical_identity is None or activity.scope.product_grant is None:
        raise InferenceAdmissionError("Native broker requests require original product activity authority.")
    content = request.content
    if len(content) > MAX_REQUEST_BYTES:
        raise InferenceAdmissionError("The native model request exceeds the broker request limit.")
    prefix = httpx2.URL(service.settings.litellm_api_base).path.rstrip("/") + "/"
    route = request.url.path.removeprefix(prefix)
    if route not in ROUTES:
        raise InferenceAdmissionError("The native model route is not supported.")
    # The broker never needs connector credentials to check inference authority.
    scope = replace(activity.scope, grants=[replace(grant, credential="") for grant in activity.scope.grants])
    envelope = _signer(service).dumps({
        "v": 1, "scope": SCOPE.dump_python(scope), "activity": activity.identity,
        "logical": activity.logical_identity, "ordinal": activity.requests,
        "foreground": activity.foreground,
        "body": hashlib.sha256(content).hexdigest(), "route": route,
    })
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise InferenceAdmissionError("The native inference authority envelope exceeds its bound.")
    headers = {
        "Authorization": "Bearer " + envelope, "Content-Type": "application/json",
        "Accept": request.headers.get("accept", "application/json"),
        "Accept-Encoding": "identity",
    }
    if service.settings.cloudflare_access_client_id:
        headers.update({
            "CF-Access-Client-Id": service.settings.cloudflare_access_client_id,
            "CF-Access-Client-Secret": service.settings.cloudflare_access_client_secret,
        })
    return httpx2.Request(
        "POST", service.settings.public_url.rstrip("/") + "/broker/native/v1/" + route,
        content=content,
        headers=headers,
        extensions=request.extensions,
    )


def _verify(
    service: ProductInferenceService, authorization: str, route: str, content: bytes | None,
) -> tuple[RunScope, str, str, int, bool]:
    token = authorization.removeprefix("Bearer ")
    if not authorization.startswith("Bearer ") or len(token) > MAX_ENVELOPE_BYTES:
        raise InferenceAdmissionError("A valid native broker authority envelope is required.")
    try:
        value: Any = _signer(service).loads(token, max_age=300)
        if (
            not isinstance(value, dict) or value.get("v") != 1 or value.get("route") != route
            or not isinstance(value.get("body"), str) or len(value["body"]) != 64
            or (content is not None and value["body"] != hashlib.sha256(content).hexdigest())
            or type(value.get("ordinal")) is not int or not 1 <= value["ordinal"] <= 100
            or type(value.get("foreground", False)) is not bool
            or any(not isinstance(value.get(name), str) or not 1 <= len(value[name]) <= 2000
                   for name in ("activity", "logical"))
        ):
            raise ValueError
        scope = SCOPE.validate_python(value["scope"])
        if scope.product_grant is None or any(grant.credential for grant in scope.grants):
            raise ValueError
        return scope, value["activity"], value["logical"], value["ordinal"], value.get("foreground", False)
    except (BadSignature, ValidationError, ValueError, TypeError, KeyError):
        raise InferenceAdmissionError("The native broker authority envelope is invalid or expired.") from None


async def _maintenance_response(
    service: ProductInferenceService, activity: InferenceActivity, route: str, content: bytes,
) -> Response:
    """One reclaimable owner spans admission, provider I/O, accounting and close."""
    async def perform() -> Response:
        token = INFERENCE_ACTIVITY.set(activity)
        client = httpx2.AsyncClient(
            transport=InferenceUsageTransport(require_admission=True),
            timeout=service.database.settings.model_timeout_seconds, follow_redirects=False,
        )
        upstream = None
        try:
            upstream = await client.send(client.build_request(
                "POST", service.settings.litellm_api_base.rstrip("/") + "/" + route,
                content=content, headers={
                    "Authorization": "Bearer " + service.settings.litellm_api_key,
                    "Content-Type": "application/json", "Accept-Encoding": "identity",
                },
            ), stream=True)
            body = bytearray()
            async for chunk in upstream.aiter_raw():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    raise InferenceAdmissionError("The memory-review response exceeds its bound.")
                body.extend(chunk)
            headers = {name: upstream.headers[name] for name in (
                "content-type", "content-encoding", "x-litellm-call-id", "x-request-id",
            ) if name in upstream.headers}
            return Response(bytes(body), status_code=upstream.status_code, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider errors may contain private inputs or credentials
            if activity.admission_error is not None:
                return Response(status_code=409, headers={"x-moyai-inference-denied": "1"})
            return Response(status_code=502)
        finally:
            async def close() -> None:
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    await client.aclose()
            try:
                await drain(asyncio.create_task(close()))
            finally:
                INFERENCE_ACTIVITY.reset(token)

    task = service.slots.run_maintenance(perform())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        caller = asyncio.current_task()
        if task.cancelled() and caller is not None and not caller.cancelling():
            # Foreground preemption is not permission to retry a paid request:
            # the existing ledger's logical barrier decides any later attempt.
            return Response(status_code=503, headers={"x-moyai-memory-preempted": "1"})
        if not task.done():
            task.cancel()
        async def settle() -> None:
            await asyncio.gather(task, return_exceptions=True)
        await drain(asyncio.create_task(settle()))
        raise


def product_inference_routes(service: ProductInferenceService) -> APIRouter:
    router = APIRouter()

    @router.post("/broker/native/v1/{route:path}")
    async def infer(request: Request, route: str) -> Response:
        received_at = getattr(request.state, "moyai_received_at", None) or now()
        if route not in ROUTES:
            raise HTTPException(404, "Unknown native inference route.")
        if service.remote:
            raise HTTPException(503, "This process does not own native broker dispatch.")
        authorization = request.headers.get("authorization", "")
        try:
            _verify(service, authorization, route, None)
        except InferenceAdmissionError:
            return Response(status_code=403, headers={"x-moyai-inference-denied": "1"})
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, "The model request exceeds the broker request limit.")
            content.extend(chunk)
        try:
            scope, identity, logical, ordinal, foreground = _verify(
                service, authorization, route, bytes(content),
            )
            if scope.surface == "memory_review":
                if service.memory_review is None:
                    raise InferenceAdmissionError("Native memory review is not configured.")
                await service.memory_review.authorize_scope(scope)
            else:
                await service.database.authorize_scope(scope)
        except (InferenceAdmissionError, PermissionError, HTTPException):
            return Response(status_code=403, headers={"x-moyai-inference-denied": "1"})
        activity = InferenceActivity(
            service.database, scope, identity, requests=ordinal - 1, logical_identity=logical,
            foreground=foreground, broker_received_at=received_at,
        )
        if scope.surface == "memory_review":
            return await _maintenance_response(service, activity, route, bytes(content))
        token = INFERENCE_ACTIVITY.set(activity)
        client = httpx2.AsyncClient(
            transport=InferenceUsageTransport(require_admission=True),
            timeout=service.database.settings.model_timeout_seconds, follow_redirects=False,
        )
        try:
            upstream = await client.send(client.build_request(
                "POST", service.settings.litellm_api_base.rstrip("/") + "/" + route,
                content=bytes(content), headers={
                    "Authorization": "Bearer " + service.settings.litellm_api_key,
                    "Content-Type": "application/json", "Accept-Encoding": "identity",
                },
            ), stream=True)
        except asyncio.CancelledError:
            await drain(asyncio.create_task(client.aclose()))
            raise
        except Exception:  # noqa: BLE001 - upstream errors can contain credentials; fixed safe response only
            await client.aclose()
            if activity.admission_error is not None:
                return Response(status_code=409, headers={"x-moyai-inference-denied": "1"})
            return Response(status_code=502)
        finally:
            INFERENCE_ACTIVITY.reset(token)

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                async def close():
                    try:
                        await upstream.aclose()
                    finally:
                        await client.aclose()
                await drain(asyncio.create_task(close()))

        # Preserve provider bytes and encoding, but forward no keys or unneeded
        # provider headers. The broker already owns the authoritative receipt.
        headers = {name: upstream.headers[name] for name in (
            "content-type", "content-encoding", "x-litellm-call-id", "x-request-id",
        ) if name in upstream.headers}
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)

    return router
