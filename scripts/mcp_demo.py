"""An authenticated inventory fixture for exercising Moyai's real MCP connection flow.

Run with ``uv run uvicorn scripts.mcp_demo:app --host 127.0.0.1 --port 8873``.
The demo bearer token is ``mcp-demo-token-only``. Model calls still use Moyai's
configured gateway; this fixture supplies only synthetic external tool data.
"""

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()
notes: list[str] = []
TOOLS = [
    {
        "name": "lookup_inventory",
        "description": "Look up current inventory and the verification code for a product SKU",
        "inputSchema": {
            "type": "object", "properties": {"sku": {"type": "string"}},
            "required": ["sku"], "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "create_test_note",
        "description": "Save a note in the isolated demo fixture",
        "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        },
        "annotations": {"readOnlyHint": False},
    },
]


def record(path: str, receipt: str) -> None:
    with Path(path).open("a") as handle:
        handle.write(receipt + "\n")


@app.post("/mcp")
async def rpc(request: Request) -> Response:
    if request.headers.get("authorization") != "Bearer mcp-demo-token-only":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    payload = await request.json()
    if "id" not in payload:
        return Response(status_code=202)
    match payload.get("method"):
        case "initialize":
            result = {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "Moyai inventory demo", "version": "1.0"},
            }
        case "tools/list":
            result = {"tools": TOOLS}
        case "ping":
            result = {}
        case "tools/call":
            params = payload["params"]
            if receipt_path := os.environ.get("MOYAI_MCP_DEMO_RECEIPTS"):
                await asyncio.to_thread(record, receipt_path, json.dumps({"method": "tools/call", "params": params}))
            if params["name"] == "lookup_inventory":
                content = json.dumps({
                    "sku": params["arguments"]["sku"], "available": 42,
                    "warehouse": "Brooklyn", "verification_code": "MOYAI-MCP-4821",
                })
            elif params["name"] == "create_test_note":
                notes.append(params["arguments"]["text"])
                content = "Demo note saved"
            else:
                return JSONResponse({
                    "jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Unknown tool"},
                })
            result = {"content": [{"type": "text", "text": content}], "isError": False}
        case _:
            return JSONResponse({
                "jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Unknown method"},
            })
    return JSONResponse({"jsonrpc": "2.0", "id": payload["id"], "result": result})


@app.delete("/mcp")
async def close() -> Response:
    return Response(status_code=200)
