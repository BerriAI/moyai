"""Isolated external Skills/MCP data for local Moyai E2E.

Run through the approved E2E launcher with uvicorn on 127.0.0.1:8874. Connect the
actual Moyai app to that URL using ``gateway-demo-token-only``. This is an external
provider fixture, not a substitute Moyai application or a scripted model.
"""

from __future__ import annotations

import io
import json
import zipfile

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, Response

TOKEN = "gateway-demo-token-only"
NAME = "gateway-verification"
MANIFEST = """---
name: gateway-verification
description: Verify a gateway connection using its inventory tool.
---
Use tool search to find lookup_inventory, then call it with SKU MOYAI-42.
Report the observed available quantity and verification code.
End your response with the exact sentence: Explicit skill selection verified.
Do not use the computer or shell for this task.
"""
receipts: list[dict] = []
uploads: list[str] = []


app = FastAPI()


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.headers.get("authorization") != "Bearer " + TOKEN:
        return Response(status_code=401)
    if request.url.path != "/receipts":
        receipts.append({"method": request.method, "path": request.url.path})
    return await call_next(request)


def metadata():
    return {"id": NAME, "name": NAME, "description": "Verify an explicitly invoked skill and MCP tool search.",
            "version": "1.0.0", "updated_at": "2026-11-16T00:00:00Z"}


@app.get("/skill-hub/skills")
async def skills(offset: int = 0):
    return {"skills": [metadata()] if offset == 0 else [], "count": 1, "next_offset": None}


@app.get("/skill-hub/skills/{name}/archive")
async def archive(name: str):
    if name != NAME:
        return Response(status_code=404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("SKILL.md", MANIFEST)
        package.writestr("references/check.txt", "This package is synthetic E2E input data.")
    return Response(buffer.getvalue(), media_type="application/zip")


@app.get("/skill-hub/skills/{name}")
async def detail(name: str):
    return metadata() if name == NAME else Response(status_code=404)


@app.post("/skill-hub/skills/upload")
async def upload(file: UploadFile):
    content = await file.read()
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        assert "SKILL.md" in package.namelist()
    uploads.append(file.filename or "skill.zip")
    return {"status": "pending_review", "action": "submitted", "submission_id": "isolated-demo-submission"}


@app.post("/mcp")
async def mcp(request: Request):
    message = await request.json()
    if "id" not in message:
        return Response(status_code=202)
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "Isolated gateway demo", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{
            "name": "lookup_inventory", "description": "Look up inventory availability and verification code by product SKU.",
            "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
            "annotations": {"readOnlyHint": True},
        }]}
    elif method == "tools/call":
        receipts.append({"method": "tools/call", "name": message["params"]["name"]})
        result = {"content": [{"type": "text", "text": json.dumps({
            "sku": message["params"]["arguments"]["sku"], "available": 42, "verification_code": "GATEWAY-VERIFIED-4821",
        })}], "isError": False}
    else:
        result = {}
    return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})


@app.delete("/mcp")
async def close():
    return Response(status_code=200)


@app.get("/receipts")
async def recorded():
    return {"requests": receipts, "uploads": uploads}
