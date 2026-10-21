"""Persistent OCU stdio worker, supervised by browser_driver in its own state directory.

No shell commands or model loop. A lost worker invalidates its observations;
the supervisor never resends a request after dispatch.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

MAX_FRAME = 16 * 1024 * 1024
SCREENSHOTS = Path("/workspace/.moyai/desktop-screenshots")
ACTIONS = {"click", "drag", "set_value", "type_text", "press_key", "scroll", "perform_secondary_action"}
SYSTEM_PYTHON = "/usr/bin/python3"
NOT_DISPATCHED = "Moyai native input not dispatched: "


class InputNotDispatched(RuntimeError):
    def __str__(self):
        return NOT_DISPATCHED + super().__str__()


class Ocu:
    def __init__(self):
        # OCU 1.1.2 invokes bare python3 for its Linux AT-SPI helper. Debian's
        # python3-gi belongs to system Python, not Modal's /usr/local Python.
        # Override only Python in this child, preserving Node and workspace PATH.
        # Create this at startup so saved computers receive the fix too.
        self.python_bin = tempfile.TemporaryDirectory(prefix="moyai-ocu-python-")
        launcher = Path(self.python_bin.name, "python3")
        launcher.write_text(
            f"#!{SYSTEM_PYTHON}\n"
            "import os, pathlib, runpy, sys\n"
            "if (len(sys.argv) == 3 and pathlib.Path(sys.argv[1]).name == 'runtime.py'\n"
            "    and pathlib.Path(sys.argv[1]).parent.name.startswith('open-computer-use-linux-')):\n"
            f"    runpy.run_path({str(Path(__file__).resolve())!r})['linux_runtime'](*sys.argv[1:])\n"
            "else:\n"
            f"    os.execv({SYSTEM_PYTHON!r}, [{SYSTEM_PYTHON!r}, *sys.argv[1:]])\n"
        )
        launcher.chmod(0o700)
        env = {**os.environ, "PATH": self.python_bin.name + os.pathsep + os.environ.get("PATH", os.defpath)}
        self.process = subprocess.Popen(
            ["ocu", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
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
            text = content(result)["text"]
            if text.startswith(NOT_DISPATCHED):
                return {**self.observe(app, context), "action_executed": False,
                        "message": text.removeprefix(NOT_DISPATCHED)}
            # OCU can fail after sending input (for example while reading the
            # new state). Its error flag alone never proves non-execution.
            raise RuntimeError("OCU did not confirm the action; inspect before repeating it.")
        # A successful action is not made uncertain by an unsuccessful later read.
        try:
            current = self.observe(app, context)
        except (OSError, ValueError, KeyError, RuntimeError):
            current = {"available": False, "message": "The action returned successfully; read the app to verify."}
        return {**current, "action_executed": True}


class LinuxInput:
    """Input corrections for the pinned OCU 1.1.2 Linux runtime.

    Keep OCU's app/element identities and snapshots. Never search for an arbitrary
    editable field or fall back to global typing after a rejected/partial edit.
    This adapter runs inside OCU's short-lived distro-Python helper, so the same
    versioned guest update repairs existing computers as well as new images.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.operation = runtime.perform_operation
        self.snapshot = runtime.build_snapshot

    def nodes(self, window, predicate):
        return [node for node in self.runtime.iter_all(window) if predicate(node)]

    def focused(self, window):
        r = self.runtime
        nodes = self.nodes(window, lambda node: r.state_contains(node, r.Atspi.StateType.FOCUSED))
        if len(nodes) != 1:
            raise InputNotDispatched("Read and focus one control in the requested app before sending keyboard input.")
        return nodes[0]

    def editable(self, node):
        r = self.runtime
        return r.supports_interface(node, "EditableText") and r.supports_interface(node, "Text")

    def text_state(self, node):
        api = self.runtime.Atspi.Text
        iface = node.get_text_iface()
        value = api.get_text(iface, 0, -1)
        caret = api.get_caret_offset(iface)
        count = api.get_n_selections(iface)
        if count > 1:
            raise InputNotDispatched("Multiple text selections are not supported; inspect the app before editing.")
        selection = api.get_selection(iface, 0) if count else None
        start, end = sorted((selection.start_offset, selection.end_offset)) if selection else (caret, caret)
        if not 0 <= start <= end <= len(value) or not 0 <= caret <= len(value):
            raise InputNotDispatched("The app returned an invalid text cursor or selection.")
        return iface, value, caret, start, end

    @staticmethod
    def confirmed(result):
        if not result:
            raise RuntimeError("The app did not confirm input. Inspect its state before repeating the action.")

    def type_text(self, window, text):
        r = self.runtime
        node = self.focused(window)
        if not self.editable(node) or not r.state_contains(node, r.Atspi.StateType.EDITABLE):
            raise InputNotDispatched("The focused control does not expose editable text. No keyboard fallback was sent.")
        iface, before, _, start, end = self.text_state(node)
        if not text:
            return
        editable = node.get_editable_text_iface()
        if end > start:
            self.confirmed(r.Atspi.EditableText.delete_text(editable, start, end))
        self.confirmed(r.Atspi.EditableText.insert_text(editable, start, text, len(text.encode("utf-8"))))
        expected = before[:start] + text + before[end:]
        if r.Atspi.Text.get_text(iface, 0, -1) != expected:
            raise RuntimeError("The app did not retain the requested text. Inspect before repeating the action.")
        self.confirmed(r.Atspi.Text.set_caret_offset(iface, start + len(text)))

    def press_key(self, window, key):
        r = self.runtime
        self.focused(window)
        if not r.state_contains(window, r.Atspi.StateType.ACTIVE):
            raise InputNotDispatched("The requested app is not active; no global key was sent. Focus it first.")
        parts = str(key).split("+")
        if not parts or any(not part for part in parts):
            raise InputNotDispatched("Invalid key combination.")
        mask = 0
        for modifier in parts[:-1]:
            name = r.MODIFIER_BITS.get(modifier.lower())
            if name is None:
                raise InputNotDispatched("Unsupported key modifier.")
            mask |= 1 << int(getattr(r.Atspi.ModifierType, name))
        normalized = r.KEY_ALIASES.get(parts[-1].lower(), parts[-1])
        try:
            symbol = r.keyval(normalized)  # Validate before locking any modifiers.
        except RuntimeError as error:
            raise InputNotDispatched(str(error)) from error
        synth = r.Atspi.KeySynthType
        send = r.Atspi.generate_keyboard_event
        try:
            if mask:
                self.confirmed(send(mask, None, synth.LOCKMODIFIERS))
            self.confirmed(send(symbol, None, synth.SYM))
        finally:
            if mask:
                self.confirmed(send(mask, None, synth.UNLOCKMODIFIERS))

    def scroll(self, element, direction, pages):
        """Use the requested control's scrollbar; never PageDown another field."""
        r = self.runtime
        orientation = r.Atspi.StateType.HORIZONTAL if direction in {"left", "right"} else r.Atspi.StateType.VERTICAL
        bars = self.nodes(element, lambda node: r.node_role(node) in {"scroll bar", "scrollbar"}
                          and r.supports_interface(node, "Value") and r.state_contains(node, orientation))
        if len(bars) != 1:
            raise InputNotDispatched("Choose a control with one accessible scrollbar in the requested direction.")
        bar = bars[0].get_value_iface()
        api = r.Atspi.Value
        before = api.get_current_value(bar)
        frame = r.extents(element)
        size = frame["width" if direction in {"left", "right"} else "height"]
        step = max(api.get_minimum_increment(bar), size * .9) * pages
        target = max(api.get_minimum_value(bar), min(api.get_maximum_value(bar),
                     before + (-step if direction in {"left", "up"} else step)))
        self.confirmed(api.set_current_value(bar, target))
        if abs(api.get_current_value(bar) - target) > 1:
            raise RuntimeError("The app did not confirm the requested scroll position.")

    def perform(self, operation):
        r = self.runtime
        action = operation.get("tool")
        if action not in {"type_text", "press_key", "scroll", "click"}:
            return self.operation(operation)
        app = r.resolve_app(operation["app"])
        _, window = r.main_window(app)
        element = r.find_element(app, operation.get("element"))
        if action == "click":
            if (element is None or not self.editable(element)
                    or operation.get("mouse_button", "left") != "left"
                    or operation.get("click_count", 1) != 1):
                return self.operation(operation)
            # GTK entries expose "activate", which can submit a form. Clicking
            # an editable element must focus it, not invoke that default action.
            self.confirmed(r.Atspi.Component.grab_focus(element.get_component_iface()))
        elif action == "type_text":
            self.type_text(window, operation.get("text", ""))
        elif action == "press_key":
            self.press_key(window, operation.get("key", ""))
        else:
            if element is None:
                raise InputNotDispatched("The observed scroll target is unavailable.")
            self.scroll(element, operation.get("direction", "down"), operation.get("pages", 1))
        if operation.get("skip_read_back", False):
            return {"ok": True}
        return {"ok": True, "snapshot": r.build_snapshot(operation["app"])}

    def build_snapshot(self, *args, **kwargs):
        result = self.snapshot(*args, **kwargs)
        r = self.runtime
        _, window = r.main_window(r.resolve_app(args[0]))
        focused = self.nodes(window, lambda node: r.state_contains(node, r.Atspi.StateType.FOCUSED))
        if len(focused) == 1 and self.editable(focused[0]):
            node = focused[0]
            iface = node.get_text_iface()
            caret = r.Atspi.Text.get_caret_offset(iface)
            selections = [
                r.Atspi.Text.get_selection(iface, index)
                for index in range(r.Atspi.Text.get_n_selections(iface))
            ]
            ranges = [(selection.start_offset, selection.end_offset) for selection in selections]
            path = []
            for _ in range(64):
                if node == window:
                    break
                path.append(node.get_index_in_parent())
                node = node.get_parent()
                if node is None:
                    break
            # OCU renders focusedSummary verbatim. Cursor/selection and identity
            # changes now invalidate observations even when the text is unchanged.
            result["focusedSummary"] = (
                (result.get("focusedSummary") or "") +
                f" [path={list(reversed(path))}; caret={caret}; selections={ranges}]"
            )
        return result


def linux_runtime(script, operation):
    """Adapt the pinned embedded runtime without changing the installed binary."""
    spec = importlib.util.spec_from_file_location("moyai_ocu_linux", script)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    adapter = LinuxInput(runtime)
    runtime.perform_operation = adapter.perform
    runtime.build_snapshot = adapter.build_snapshot
    sys.argv = [script, operation]
    try:
        runtime.main()
    except Exception as error:  # noqa: BLE001 - OCU requires a JSON protocol response
        print(json.dumps({"ok": False, "error": str(error)}))


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
