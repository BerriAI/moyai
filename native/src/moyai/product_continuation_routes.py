"""Explicit product continuation and authenticated retained recovery downloads."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .product_continuation import ContinuationRequest
from .product_continuation_models import ProductContinuationCapture
from .product_continuation_workspace import fingerprint
from .product_files import FILE_HEADERS
from .product_history import session_id


def continuation_routes(runtime: Any, reader) -> APIRouter:
    router = APIRouter()
    service = runtime.product_continuations

    @router.get("/api/product/environments")
    async def environments(current=Depends(reader)):
        if current.principal is None:
            raise HTTPException(401, "Current product authorization is required.")
        return await runtime.product_workspace.catalog(current.principal)

    @router.get("/api/product/sessions/{run_id}/continuation")
    async def preview(run_id: str, current=Depends(reader)):
        if current.principal is None:
            raise HTTPException(401, "Current product authorization is required.")
        return await service.preview(current.principal, run_id)

    @router.post("/api/product/sessions/{run_id}/continuation")
    async def continue_session(
        run_id: str, body: ContinuationRequest, request: Request, current=Depends(reader),
    ):
        # actor() may check native CSRF; the original cookie's CSRF and Origin
        # are independently required for this cross-source admission.
        principal = await runtime.product_identity.authenticate(request, mutation=True)
        if current.principal is None or principal.grant != current.principal.grant:
            raise HTTPException(401, "The product requester changed.")
        if body.expected_source_fingerprint is None:
            raise HTTPException(422, "Review the source continuation preview before sending.")
        return await service.continue_session(
            principal, current.user, run_id, body,
            received_at=getattr(request.state, "moyai_received_at", None),
        )

    async def allowed(current, run_id: str):
        if current.principal is None:
            raise HTTPException(401, "Current product authorization is required.")
        principal = await runtime.product_identity.require_current(current.principal.grant)
        await runtime.product_history.require(principal, run_id)
        async with runtime.database.sessions() as db:
            binding = await service.lookup(db, run_id, principal)
            if binding is None:
                raise HTTPException(404, "This session has no native continuation.")
            return binding

    @router.get("/api/product/sessions/{run_id}/continuation/files")
    async def files(run_id: str, current=Depends(reader)):
        binding = await allowed(current, run_id)
        async with runtime.database.sessions() as db:
            rows = list((await db.scalars(select(ProductContinuationCapture).where(
                ProductContinuationCapture.native_session_id == binding.native_session_id,
            ).order_by(ProductContinuationCapture.source_session_id).limit(1001))).all())
        if len(rows) > 1000:
            raise HTTPException(503, "The recovery inventory exceeds its supported bound.")
        result = []
        for row in rows:
            descriptor = json.loads(runtime.security.unseal(row.descriptor))
            if fingerprint(descriptor) != row.fingerprint:
                raise HTTPException(503, "The saved recovery descriptor failed integrity verification.")
            url = f"/api/product/sessions/{run_id}/continuation/archive/{row.source_session_id}"
            result.append({
                "kind": "artifact", "id": "recovery:" + row.source_session_id,
                "name": "Earlier workspace " + row.source_session_id[:8] + ".zip",
                "url": url, "download_url": url, "size": descriptor["size"],
                "media_type": "application/zip", "source_format": "native-recovery-chunks-v1",
                "preview_supported": False, "restored_directory": "/workspace/previous/" + row.source_session_id,
                "omissions": descriptor["manifest"].get("note", ""),
            })
        await allowed(current, run_id)
        return {"files": result, "sources": [], "links": [], "source_session_id": run_id,
                "original_files_url": f"/api/product/sessions/{run_id}/files"}

    @router.get("/api/product/sessions/{run_id}/continuation/archive/{member_id}")
    async def archive(run_id: str, member_id: str, current=Depends(reader)):
        session_id(member_id)
        binding = await allowed(current, run_id)
        await runtime.product_history.require(current.principal, member_id)
        async with runtime.database.sessions() as db:
            row = await db.get(ProductContinuationCapture, (binding.native_session_id, member_id))
            if row is None:
                raise HTTPException(404, "Recovery archive not found.")
            descriptor = json.loads(runtime.security.unseal(row.descriptor))
            if fingerprint(descriptor) != row.fingerprint:
                raise HTTPException(503, "Recovery archive metadata failed integrity verification.")

        async def chunks():
            checksum, size = hashlib.sha256(), 0
            for part in descriptor["parts"]:
                await allowed(current, run_id)
                await runtime.product_history.require(current.principal, member_id)
                raw = await runtime.objects.get(part["digest"])
                if len(raw) != part["size"]:
                    raise ValueError("Recovery object length failed integrity verification.")
                await allowed(current, run_id)
                await runtime.product_history.require(current.principal, member_id)
                checksum.update(raw)
                size += len(raw)
                yield raw
            if size != descriptor["size"] or checksum.hexdigest() != descriptor["sha256"]:
                raise ValueError("Recovery archive failed integrity verification.")
        await allowed(current, run_id)
        return StreamingResponse(chunks(), media_type="application/zip", headers={
            **FILE_HEADERS, "Content-Length": str(descriptor["size"]),
            "Content-Disposition": f'attachment; filename="earlier-workspace-{member_id}.zip"',
        })

    return router
