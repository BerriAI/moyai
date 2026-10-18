"""Persistent OCU stdio worker, supervised by browser_driver in its own state directory.

No shell commands or model loop. A lost worker invalidates its observations;
the supervisor never resends a request after dispatch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

MAX_FRAME = 16 * 1024 * 1024
SCREENSHOTS = Path("/workspace/.moyai/desktop-screenshots")
ACTIONS = {"click", "drag", "set_value", "type_text", "press_key", "scroll", "perform_secondary_action"}


class Ocu:
    def __init__(self):
        self.process = subprocess.Popen(
            ["ocu", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.sequence = 0
        self.observation = None
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "moyai", "version": "1"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.state_options = next(tool["inputSchema"]["properties"] for tool in
                                  self.rpc("tools/list", {})["tools"] if tool["name"] == "get_app_state")

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        while True:
            frame = self.process.stdout.readline(MAX_FRAME + 1)
            if not frame or len(frame) > MAX_FRAME or not frame.endswith(b"\n"):
                raise RuntimeError("OCU returned an incomplete or oversized response.")
            response = json.loads(frame)
            if "id" not in response:
                continue
            if response["id"] != self.sequence or "error" in response:
                raise RuntimeError("OCU could not confirm the request.")
            return response["result"]

    def call(self, tool, args):
        return self.rpc("tools/call", {"name": tool, "arguments": args})

    def state(self, app, *, screenshot=False):
        args = {"app": app, "text_limit": "max"}
        # The pinned Linux 1.1.2 binary always captures screenshots. Do not
        # advertise unsupported flags or feed their base64 bytes to the model.
        if "include_screenshot" in self.state_options:
            args["include_screenshot"] = screenshot
        return self.call("get_app_state", args)

    def observe(self, app, context, *, screenshot=False):
        result = self.state(app, screenshot=screenshot)
        self.observation = None
        value = content(result, images=screenshot)
        if result.get("isError"):
            return {**value, "available": False}
        token = str(uuid4())
        self.observation = (token, app, context, value["text"], screenshot, value["images"])
        return {**value, "available": True, "observation": token}

    def perform(self, command, deadline):
        action, app = command["action"], command.get("app", "")
        context = command.get("context", "")
        if action == "start" or (action == "read" and not app):
            result = self.call("list_apps", {})
            return {**content(result), "available": not result.get("isError", False)}
        if action == "read":
            return self.observe(app, context, screenshot=command.get("screenshot", False))
        if action not in ACTIONS:
            raise ValueError("Unsupported desktop action.")
        previous = self.observation
        coordinates = action == "drag" or (action == "click" and not command.get("element_index"))
        current = self.observe(app, context, screenshot=coordinates)
        if (previous is None or previous[:3] != (command.get("observation"), app, context)
                or not current["available"] or previous[3] != current["text"]):
            return {**current, "action_executed": False,
                    "message": "The observation changed or expired. Inspect this state before choosing an action."}
        if coordinates and not previous[4]:
            return {**current, "action_executed": False,
                    "message": "Inspect the screenshot before choosing coordinates."}
        if coordinates and (not current["images"] or previous[5] != current["images"]):
            return {**current, "action_executed": False,
                    "message": "The screenshot changed. Inspect it before choosing new coordinates."}
        if command.get("element_index") and command["element_index"] not in re.findall(
            r"^\s*(\d+) \S", current["text"], re.MULTILINE,
        ):
            return {**current, "action_executed": False, "message": "Choose an element index from this observation."}
        args = {"app": app}
        fields = {"click": ("element_index", "x", "y", "click_count", "mouse_button"),
                  "drag": ("from_x", "from_y", "to_x", "to_y"), "set_value": ("element_index", "value"),
                  "type_text": ("text",), "press_key": ("key",),
                  "scroll": ("element_index", "direction", "pages"),
                  "perform_secondary_action": ("element_index",)}
        for key in fields[action]:
            if key in command and command[key] is not None:
                args[key] = command[key]
        if action == "perform_secondary_action":
            args["action"] = command["secondary_action"]
        if time.time() >= deadline:
            raise TimeoutError("The desktop action expired before dispatch.")
        self.observation = None
        result = self.call(action, args)
        if result.get("isError"):
            # OCU can fail after sending input (for example while reading the
            # new state). Its error flag alone never proves non-execution.
            raise RuntimeError("OCU did not confirm the action; inspect before repeating it.")
        # A successful action is not made uncertain by an unsuccessful later read.
        try:
            current = self.observe(app, context)
        except (OSError, ValueError, KeyError, RuntimeError):
            current = {"available": False, "message": "The action returned successfully; read the app to verify."}
        return {**current, "action_executed": True}


def content(result, *, images=False):
    """Preserve text, represent images as files, never tokenize base64 PNGs."""
    text, paths = [], []
    for part in result.get("content", []):
        if part.get("type") == "text":
            text.append(part["text"])
        elif part.get("type") == "image":
            if not images:
                continue
            if part.get("mimeType") != "image/png":
                raise ValueError("Unsupported desktop image format.")
            raw = base64.b64decode(part["data"], validate=True)
            SCREENSHOTS.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = SCREENSHOTS / (hashlib.sha256(raw).hexdigest() + ".png")
            path.write_bytes(raw)
            paths.append(str(path))
        else:
            raise ValueError("Unsupported OCU response content.")
    return {"text": "\n".join(text), "images": paths}


def worker():
    ocu = None
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if time.time() >= message["deadline"]:
                raise TimeoutError("The desktop request expired.")
            if not shutil.which("ocu"):
                result = {"available": False, "action_executed": False,
                          "message": "This computer image does not include OCU. A new OCU-enabled image is required; "
                                     "do not install it at runtime or discard this computer's files."}
            else:
                if ocu is None:
                    ocu = Ocu()
                result = ocu.perform(message["command"], message["deadline"])
            print(json.dumps({"result": result}), flush=True)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            print(json.dumps({"error": True}), flush=True)
            return


if __name__ == "__main__" and sys.argv[1:] == ["worker"]:
    worker()
