"""Small legacy MCP stdio server: run-scoped app tools plus an isolated Chromium browser.

Uses newline-delimited JSON-RPC and never writes non-protocol output to stdout.
Inference provider keys stay server-side. Approved generic access is scoped to
one subprocess by credential_tools and never returned as raw tool output.
"""
import json
import os
import sys
import urllib.request
from urllib.error import HTTPError

try:
    from . import github_tools, computer, credential_tools
except ImportError:
    import github_tools
    import computer
    import credential_tools

BROKER = os.environ.get("WORKSPACE_BROKER_URL", "")
TOKEN = os.environ.get("WORKSPACE_RUN_TOKEN", "")
GIT_BROKER = os.environ.get('WORKSPACE_GIT_BROKER_URL', BROKER)


def broker(path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BROKER + path, data=data, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=920) as response:
        return json.load(response)


def browser_tool(name, args):
    action = name.removeprefix('browser_')
    if name not in {tool['name'] for tool in BROWSER_TOOLS}:
        raise ValueError('Unknown browser tool')
    return computer.request({'action': action, 'actor': 'agent', 'args': args})


def schema(properties, required):
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in properties.items()}, "required": required, "additionalProperties": False}


BROWSER_TOOLS = [
    {"name": "browser_open", "description": "Open an HTTP(S) page in the sandbox's isolated Chromium browser.", "inputSchema": schema({"url": "URL to open"}, ["url"])},
    {"name": "browser_read", "description": "Read the current browser page and save its screenshot.", "inputSchema": schema({}, [])},
    {"name": "browser_click", "description": "Click a visible element by its accessible role and exact name. Can perform website actions.", "inputSchema": schema({"role": "Accessible role, such as button or link", "name": "Exact accessible name"}, ["role", "name"])},
    {"name": "browser_fill", "description": "Fill a form field by its exact accessible label.", "inputSchema": schema({"label": "Field's accessible label", "value": "Text to enter"}, ["label", "value"])},
    {"name": "browser_screenshot", "description": "Save a named PNG screenshot of the current browser viewport. Return its workspace path as a Markdown link so the user can preview/download it.", "inputSchema": schema({"name": "Short descriptive capture name"}, ["name"])},
    {"name": "browser_record_start", "description": "Start recording the sandbox browser flow as WebM. Open the page first, then start recording, perform the flow, and stop recording. No audio. Maximum 10 minutes/25 MB per clip.", "inputSchema": schema({"name": "Short descriptive recording name"}, ["name"])},
    {"name": "browser_record_stop", "description": "Stop and save the browser video. Link the returned workspace path in your answer. Active recordings are also finalized when your response ends.", "inputSchema": schema({}, [])},
    {"name": "browser_key", "description": "Press a browser key: Enter, Tab, Shift+Tab, Escape, Backspace, Delete, ArrowUp/Down/Left/Right, Control+a or Space.", "inputSchema": schema({"key": "Key to press"}, ["key"])},
    {"name": "browser_scroll", "description": "Scroll the browser viewport vertically.", "inputSchema": {"type":"object", "properties":{"dy":{"type":"integer", "description":"Pixels to scroll, negative for up"}}, "required":["dy"], "additionalProperties":False}},
]


def serve():
    tools = None
    for line in sys.stdin:
        request = None
        try:
            if len(line) > 2_000_000:
                raise ValueError("Message too large")
            request = json.loads(line)
            if "id" not in request:
                continue
            method, params = request.get("method"), request.get("params", {})
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "hermes-workspace", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = github_tools.advertised_tools(broker("/tools")) + BROWSER_TOOLS
                result = {"tools": tools}
            elif method == "tools/call":
                name, args = params["name"], params.get("arguments", {})
                try:
                    if name == 'credentials_run':
                        available = broker('/tools')
                        if name not in {tool['name'] for tool in available}:
                            raise github_tools.GitHubToolError('Credential commands are not enabled for this session.')
                        data = credential_tools.run(args, broker)
                    elif name in {'github_checkout', 'github_create_pull_request'}:
                        available = broker('/tools')  # Recheck revocation/read-only changes.
                        if name not in {tool['name'] for tool in available}:
                            raise github_tools.GitHubToolError('This GitHub operation is not enabled for the session.')
                        data = github_tools.call(name, args, broker, GIT_BROKER, TOKEN)
                    else:
                        data = browser_tool(name, args) if name.startswith("browser_") else broker("/tools/call", {"name": name, "arguments": args})
                    result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": bool(isinstance(data, dict) and data.get("error"))}
                except github_tools.GitHubToolError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                except HTTPError as exc:
                    message = f'Tool failed (HTTP {exc.code}). The action was not confirmed.'
                    if name in {'skills_load', 'skills_save', 'skills_read_file'} or name.startswith(('credentials_', 'memory_')):
                        # The skill API supplies sanitized permission/conflict
                        # messages. Preserve them so the agent can correct its
                        # arguments instead of repeating an unexplained failure.
                        try:
                            detail = json.loads(exc.read(8192)).get('detail')
                            if isinstance(detail, str):
                                message += ' ' + detail[:1000]
                        except (ValueError, AttributeError):
                            pass
                    result = {"content": [{"type": "text", "text": message}], "isError": True}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": f"Tool failed ({type(exc).__name__}). The action was not confirmed; do not retry writes automatically."}], "isError": True}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not supported"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except Exception:
            print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": "Workspace tool server error"}}), flush=True)


if __name__ == "__main__":
    serve()
