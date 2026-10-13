"""Small legacy MCP stdio server: run-scoped app tools plus an isolated Chromium browser.

Uses newline-delimited JSON-RPC and never writes non-protocol output to stdout.
Inference provider keys stay server-side. Approved generic access is scoped to
one subprocess by credential_tools and never returned as raw tool output.
"""
import json
import base64
import os
from pathlib import Path
import sys
import urllib.request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock

# MCP runtimes launch this file directly with an arbitrary working directory.
if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.tools import github_tools
from agent.tools import artifact_tools
from agent.tools import computer_tools
from sandbox import computer
from agent.tools import credential_tools
from agent.repository_guidance import TOOL as GUIDANCE_TOOL, resolve as repository_guidance
from sandbox.broker_transport import send_payload
from sandbox.broker_failure import exception_diagnostic

BROKER = os.environ.get("WORKSPACE_BROKER_URL", "")
TOKEN = os.environ.get("WORKSPACE_RUN_TOKEN", "")
GIT_BROKER = os.environ.get('WORKSPACE_GIT_BROKER_URL', BROKER)


def broker(path, body=None):
    if path == '/tools/call' and body is not None:
        return send_payload(path, body, _broker)
    return _broker(path, body)


def _broker(path, body=None):
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
    {"name": "browser_open", "description": "Open an HTTP(S) page in the sandbox browser. Set new_tab to preserve the current page.", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "new_tab": {"type": "boolean", "default": False}}, "required": ["url"], "additionalProperties": False}},
    {"name": "browser_read", "description": "Read a complete observed frame with exact DOM targets and a frame directory. Optional ranges read the same snapshot without mixing revisions; omit snapshot for a fresh observation. Text extraction does not describe canvas or image contents; use browser_screenshot for visual inspection.", "inputSchema": {"type": "object", "properties": {"frame": {"type": "string"}, "snapshot": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "characters": {"type": "integer", "minimum": 1}, "element_offset": {"type": "integer", "minimum": 0}, "element_count": {"type": "integer", "minimum": 1}}, "additionalProperties": False}},
    {"name": "browser_click", "description": "Click a visible element by its accessible role and exact name. Can perform website actions.", "inputSchema": schema({"role": "Accessible role, such as button or link", "name": "Exact accessible name"}, ["role", "name"])},
    {"name": "browser_fill", "description": "Fill a form field by its exact accessible label.", "inputSchema": schema({"label": "Field's accessible label", "value": "Text to enter"}, ["label", "value"])},
    {"name": "browser_screenshot", "description": "Save a named PNG screenshot of the current desktop, including browser tabs and address bar. Return its workspace path as a Markdown link so the user can preview/download it.", "inputSchema": schema({"name": "Short descriptive capture name"}, ["name"])},
    {"name": "browser_record_start", "description": "Start recording the sandbox browser flow as WebM. Open the page first, then start recording, perform the flow, and stop recording. No audio. Maximum 10 minutes/25 MB per clip.", "inputSchema": schema({"name": "Short descriptive recording name"}, ["name"])},
    {"name": "browser_record_stop", "description": "Stop and save the browser video. Link the returned workspace path in your answer. Active recordings are also finalized when your response ends.", "inputSchema": schema({}, [])},
    {"name": "browser_key", "description": "Press a key or combination using Playwright keyboard syntax, such as Enter, Control+Home or Shift+Tab.", "inputSchema": schema({"key": "Key to press"}, ["key"])},
    {"name": "browser_scroll", "description": "Scroll the browser viewport vertically.", "inputSchema": {"type":"object", "properties":{"dy":{"type":"integer", "description":"Pixels to scroll, negative for up"}}, "required":["dy"], "additionalProperties":False}},
]

# Keep the existing role/label selectors and add exact observed-node targets.
for tool in BROWSER_TOOLS:
    if tool['name'] in {'browser_click', 'browser_fill'}:
        properties = tool['inputSchema']['properties']
        properties.update(target={'type': 'string', 'description': 'Exact target from the latest observed page/frame.'}, frame={'type': 'string'})
        tool['inputSchema']['required'] = ['value'] if tool['name'] == 'browser_fill' else []
        tool['inputSchema']['anyOf'] = [{'required': ['target']}, {'required': ['role', 'name'] if tool['name'] == 'browser_click' else ['label']}]
        if tool['name'] == 'browser_click':
            properties.update(x={'type': 'number'}, y={'type': 'number'})
            tool['inputSchema']['anyOf'].append({'required': ['x', 'y']})
    if tool['name'] == 'browser_scroll':
        tool['inputSchema']['properties']['dx'] = {'type': 'integer', 'default': 0}
BROWSER_TOOLS += [
    {'name': 'browser_tabs', 'description': 'List all tabs in the agent browser, including popups, with stable IDs.', 'inputSchema': schema({}, [])},
    {'name': 'browser_type', 'description': 'Insert complete text at the currently focused browser control.', 'inputSchema': schema({'text': 'Text to insert'}, ['text'])},
    {'name': 'browser_select_tab', 'description': 'Select a tab by its observed ID.', 'inputSchema': schema({'page_id': 'ID returned by browser_tabs'}, ['page_id'])},
    *[{'name': 'browser_'+action, 'description': description, 'inputSchema': schema({}, [])}
      for action, description in [('back', 'Navigate back.'), ('forward', 'Navigate forward.'), ('reload', 'Reload the current page.')]],
    {'name': 'browser_select', 'description': 'Select option values on an observed select element.', 'inputSchema': {'type': 'object', 'properties': {'target': {'type': 'string'}, 'values': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['target', 'values'], 'additionalProperties': False}},
    {'name': 'browser_upload', 'description': 'Upload existing workspace files to an observed file input. This sends files to the current website; use only for the authorized task.', 'inputSchema': {'type': 'object', 'properties': {'target': {'type': 'string'}, 'paths': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}}, 'required': ['target', 'paths'], 'additionalProperties': False}},
    {'name': 'browser_download', 'description': 'Click an observed download target and save its completed file in the workspace. Returns a path and size; publish an artifact to deliver it.', 'inputSchema': schema({'target': 'Exact download target'}, ['target'])},
]


# Audited broker reads only. Sandbox checkout changes files, browser actions
# share a page, and skills/memory searches select model context. Those and all
# unknown/new tools stay on the ordered lane, regardless of readOnlyHint.
CONCURRENT_READS = frozenset({
    'artifacts_list', 'artifact_read',
    'github_repositories', 'github_repository', 'github_rulesets', 'github_ruleset',
    'github_pull_request', 'github_pull_request_comments',
    'linear_my_issues', 'linear_teams', 'linear_search', 'linear_issue',
    'slack_search', 'slack_thread', 'slack_me', 'notion_search', 'notion_page',
})
READ_WORKERS = 4
MAX_PENDING_PER_LANE = 32


def call_tool(params, tools, *, relay=None):
    name, args = params['name'], params.get('arguments', {})
    try:
        if os.environ.get('MOYAI_REMOTE_COMPUTER') == '1':
            from agent.tools import remote_computer
            if remote_computer.requires_computer(name):
                return remote_computer.call(name, args, broker)
        if name in {tool['name'] for tool in computer_tools.TOOLS}:
            data = computer_tools.call(name, args, os.environ.get('MOYAI_WORKSPACE', '/workspace'))
        elif name == 'repository_guidance':
            data = repository_guidance(workspace=os.environ.get('MOYAI_WORKSPACE', '/workspace'), **{'paths': [], **args})
        elif name in {'artifact_publish', 'artifact_fetch'}:
            available = broker('/tools')
            if name not in {tool['name'] for tool in available}:
                raise ValueError('Artifact transfer is unavailable for this execution.')
            data = artifact_tools.call(name, args, broker, os.environ.get('MOYAI_WORKSPACE', '/workspace'))
        elif name == 'credentials_run':
            available = broker('/tools')
            if name not in {tool['name'] for tool in available}:
                raise github_tools.GitHubToolError('Credential commands are not enabled for this session.')
            data = credential_tools.run(args, broker)
        elif name in github_tools.LOCAL_TOOLS:
            available = broker('/tools')  # Recheck revocation/read-only changes.
            if name not in {tool['name'] for tool in available}:
                raise github_tools.GitHubToolError('This GitHub operation is not enabled for the session.')
            data = github_tools.call(name, args, broker, GIT_BROKER, TOKEN, **({'relay': relay} if relay is not None else {}))
        else:
            data = browser_tool(name, args) if name.startswith("browser_") else broker("/tools/call", {"name": name, "arguments": args})
        if name == 'workspace_diagnostics' and isinstance(data, dict) and not data.get('error'):
            names = sorted(tool['name'] for tool in tools) if tools is not None else None
            data['mcp_catalog'] = {'names': names, 'count': len(names) if names is not None else None,
                'scope': 'Last tools/list response from this MCP process; not the model request catalog.'}
        content = [{"type": "text", "text": json.dumps(data)}]
        if name == 'browser_screenshot' and isinstance(data, dict) and not data.get('error'):
            path = Path(data['path']).resolve()
            if path.is_relative_to(computer.CAPTURES.resolve()) and path.suffix == '.png':
                content.append({'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(path.read_bytes()).decode()})
        return {"content": content, "isError": bool(isinstance(data, dict) and data.get("error"))}
    except (github_tools.GitHubToolError, artifact_tools.ArtifactError, computer_tools.ComputerToolError) as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except (FileExistsError, FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        # These local failures have useful repair instructions and contain no
        # upstream response body. Do not turn a known filesystem rejection into
        # an unexplained, potentially executed external write.
        detail = {FileExistsError: 'The destination exists. Read it first and choose overwrite=true only when replacement is intended.',
                  FileNotFoundError: 'The path does not exist. Check its workspace-relative path.',
                  IsADirectoryError: 'The path identifies a directory; choose a regular file.',
                  PermissionError: 'The computer denied access to this path.'}[type(exc)]
        return {'content': [{'type': 'text', 'text': detail}], 'isError': True}
    except HTTPError as exc:
        message = f'Tool failed (HTTP {exc.code}). The action was not confirmed.'
        if name in {'skills_search', 'skills_load', 'skills_save', 'skills_read_file'} or name.startswith(('credentials_', 'memory_')):
            # The skill API supplies sanitized permission/conflict
            # messages. Preserve them so the agent can correct its
            # arguments instead of repeating an unexplained failure.
            try:
                detail = json.loads(exc.read(8192)).get('detail')
                if isinstance(detail, str):
                    message += ' ' + detail[:1000]
            except (ValueError, AttributeError):
                pass
        return {"content": [{"type": "text", "text": message}], "isError": True}
    except Exception as exc:
        diagnostic = {}
        try:
            diagnostic = exception_diagnostic(exc, stage='tool_dispatch')
        except Exception:
            pass
        return {"content": [{"type": "text", "text": f"Tool failed ({type(exc).__name__}). The action was not confirmed; do not retry writes automatically."}],
                "isError": True, "error_type": type(exc).__name__, "diagnostic": diagnostic}


def serve():
    tools = None
    output_lock = Lock()

    def reply(message):
        # A JSON-RPC line must never be interleaved with another worker's reply.
        with output_lock:
            print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

    def execute(request, catalog, slots):
        try:
            result = call_tool(request['params'], catalog)
            reply({'id': request['id'], 'result': result})
        except Exception:
            reply({'id': request['id'], 'error': {'code': -32603, 'message': 'Workspace tool server error'}})
        finally:
            slots.release()

    # Separate capacity keeps a slow read from occupying the ordered tool lane.
    # EOF drains accepted requests; no action is cancelled or retried implicitly.
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as reads, ThreadPoolExecutor(max_workers=1) as ordered:
        read_slots = BoundedSemaphore(MAX_PENDING_PER_LANE)
        ordered_slots = BoundedSemaphore(MAX_PENDING_PER_LANE)
        for line in sys.stdin:
            request = None
            try:
                request = json.loads(line)
                if 'id' not in request:
                    continue
                method, params = request.get('method'), request.get('params', {})
                if method == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'hermes-workspace', 'version': '0.1.0'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    tools = github_tools.advertised_tools(broker('/tools')) + BROWSER_TOOLS + [GUIDANCE_TOOL] + computer_tools.TOOLS
                    result = {'tools': tools}
                elif method == 'tools/call':
                    executor, slots = (reads, read_slots) if params['name'] in CONCURRENT_READS else (ordered, ordered_slots)
                    if slots.acquire(blocking=False):
                        try:
                            executor.submit(execute, request, tools, slots)
                        except Exception:
                            slots.release()
                            raise
                    else:
                        reply({'id': request['id'], 'result': {'isError': True, 'content': [{'type': 'text',
                            'text': 'Workspace tool server is busy. This request was not started.'}]}})
                    continue
                else:
                    reply({'id': request['id'], 'error': {'code': -32601, 'message': 'Method not supported'}})
                    continue
                reply({'id': request['id'], 'result': result})
            except Exception:
                reply({'id': request.get('id') if isinstance(request, dict) else None,
                       'error': {'code': -32603, 'message': 'Workspace tool server error'}})


if __name__ == '__main__':
    serve()
