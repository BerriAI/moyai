"""Small legacy MCP stdio server: run-scoped app tools plus an isolated Chromium browser.

Uses newline-delimited JSON-RPC and never writes non-protocol output to stdout.
Provider credentials never enter this process.
"""
import json
import os
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlparse

try:
    from . import github_tools
except ImportError:
    import github_tools

BROKER = os.environ.get("WORKSPACE_BROKER_URL", "")
TOKEN = os.environ.get("WORKSPACE_RUN_TOKEN", "")
GIT_BROKER = os.environ.get('WORKSPACE_GIT_BROKER_URL', BROKER)
browser = page = playwright = None


def broker(path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BROKER + path, data=data, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=920) as response:
        return json.load(response)


def browser_tool(name, args):
    global browser, page, playwright
    if page is None:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        page.set_default_timeout(20000)
    if name == "browser_open":
        parsed = urlparse(args["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only HTTP(S) browser URLs without credentials are allowed.")
        page.goto(args["url"], wait_until="domcontentloaded")
    elif name == "browser_click":
        page.get_by_role(args["role"], name=args["name"], exact=True).click()
    elif name == "browser_fill":
        page.get_by_label(args["label"], exact=True).fill(args["value"])
    elif name != "browser_read":
        raise ValueError("Unknown browser tool")
    Path("/artifacts").mkdir(exist_ok=True)
    page.screenshot(path="/artifacts/browser.png", full_page=False)
    return {"url": page.url, "title": page.title(), "text": page.locator("body").inner_text()[:24000],
            "elements": page.locator('a,button,input,select,textarea').evaluate_all("els => els.slice(0,60).map(e => ({tag:e.tagName,role:e.getAttribute('role'),name:e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder'),type:e.type}))"),
            "screenshot": "Latest screenshot will be included in the result archive."}


def schema(properties, required):
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in properties.items()}, "required": required, "additionalProperties": False}


BROWSER_TOOLS = [
    {"name": "browser_open", "description": "Open an HTTP(S) page in the sandbox's isolated Chromium browser.", "inputSchema": schema({"url": "URL to open"}, ["url"])},
    {"name": "browser_read", "description": "Read the current browser page and save its screenshot.", "inputSchema": schema({}, [])},
    {"name": "browser_click", "description": "Click a visible element by its accessible role and exact name. Can perform website actions.", "inputSchema": schema({"role": "Accessible role, such as button or link", "name": "Exact accessible name"}, ["role", "name"])},
    {"name": "browser_fill", "description": "Fill a form field by its exact accessible label.", "inputSchema": schema({"label": "Field's accessible label", "value": "Text to enter"}, ["label", "value"])},
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
                    if name in {'github_checkout', 'github_create_pull_request'}:
                        available = broker('/tools')  # Recheck revocation/read-only changes.
                        if name not in {tool['name'] for tool in available}:
                            raise github_tools.GitHubToolError('This GitHub operation is not enabled for the session.')
                        data = github_tools.call(name, args, broker, GIT_BROKER, TOKEN)
                    else:
                        data = browser_tool(name, args) if name.startswith("browser_") else broker("/tools/call", {"name": name, "arguments": args})
                    result = {"content": [{"type": "text", "text": json.dumps(data)}], "isError": bool(isinstance(data, dict) and data.get("error"))}
                except github_tools.GitHubToolError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": f"Tool failed ({type(exc).__name__}). The action was not confirmed; do not retry writes automatically."}], "isError": True}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not supported"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except Exception:
            print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": "Workspace tool server error"}}), flush=True)


if __name__ == "__main__":
    try:
        serve()
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()
