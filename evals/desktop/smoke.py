"""Run in the desktop eval image with this repository mounted read-only.

Exercises the actual Linux guest/OCU/Chromium stack without a model or API key.
This is a deterministic integration check, not an agent success-rate benchmark.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "native/desktop.py"
ENV = {**os.environ, "DISPLAY": ":99", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/moyai-dbus",
       "XDG_RUNTIME_DIR": "/tmp/moyai-xdg", "NO_AT_BRIDGE": "0", "GTK_A11Y": "always"}


def guest(body):
    value = subprocess.run([sys.executable, str(GUEST), json.dumps({**body, "deadline": time.time() + 25})],
                           check=True, capture_output=True, text=True, timeout=30, env=ENV)
    result = json.loads(value.stdout)
    assert not result.get("error"), result
    return result


def request(command):
    return guest({"action": "desktop", "command": {"context": "integration-test", **command}})


def target(state, role, name):
    role_pattern = r"(?:push )?button" if role == "button" else re.escape(role)
    match = re.search(r"^\s*(\d+) " + role_pattern + " " + re.escape(name) + r"(?: |$)", state["text"], re.MULTILINE)
    assert match, state
    return match.group(1)


def action(kind, role, name, **args):
    state = request({"action": "read", "app": "apps.py"})
    result = request({"action": kind, "app": "apps.py", "observation": state["observation"],
                      "element_index": target(state, role, name), **args})
    assert result.get("action_executed"), result
    return state, result


def check_native_input():
    app_name = "input_app.py"
    directory = Path("/var/lib/eval/input-proof")
    app = subprocess.Popen(
        ["/usr/bin/python3", str(ROOT / "evals/desktop/input_app.py"), str(directory)], env=ENV,
    )

    def read():
        return request({"action": "read", "app": app_name})

    def perform(kind, role=None, name=None, **args):
        state = read()
        result = request({
            "action": kind, "app": app_name, "observation": state["observation"],
            **({"element_index": target(state, role, name)} if role else {}), **args,
        })
        return state, result

    def edit(kind, role=None, name=None, **args):
        state, result = perform(kind, role, name, **args)
        assert result.get("action_executed"), result
        return state

    def saved(**expected):
        for _ in range(30):
            try:
                value = json.loads((directory / "input-state.json").read_text())
                if all(value[key] == result for key, result in expected.items()):
                    assert value["decoy"] == "DO NOT EDIT" and value["locked"] == "LOCKED", value
                    return value
            except FileNotFoundError:
                pass
            time.sleep(.1)
        raise AssertionError((expected, value))

    try:
        for _ in range(50):
            if app_name in request({"action": "read"})["text"]:
                break
            time.sleep(.1)
        else:
            raise AssertionError("The input fixture did not start.")
        edit("click", "button", "Prepare caret")
        edit("type_text", text="X")
        saved(target="abXcd", caret=3, activations=0)
        edit("click", "text", "Target text")
        saved(activations=0)
        edit("press_key", key="Home")
        edit("press_key", key="Right")
        edit("type_text", text="é🙂")
        saved(target="aé🙂bXcd")
        edit("press_key", key="ctrl+a")
        edit("type_text", text="Renée🙂")
        saved(target="Renée🙂", caret=6)
        edit("click", "button", "Prepare selection")
        old = edit("type_text", text="Ω🙂")
        saved(target="aΩ🙂d", caret=3)
        refused = request({"action": "click", "app": app_name, "observation": old["observation"],
                           "element_index": target(old, "text", "Target text")})
        assert refused["action_executed"] is False
        edit("click", "text", "Read only text")
        _, refused = perform("type_text", text="SHOULD_NOT_APPEAR")
        assert refused["action_executed"] is False
        saved(target="aΩ🙂d")
        edit("click", "text", "Target text")
        edit("scroll", "scroll pane", "Independent scroll area", direction="down", pages=1)
        assert saved(target="aΩ🙂d", notes="first\nsecond")["scroll"] > 0
        edit("click", "text", "Multiline text")
        edit("press_key", key="ctrl+a")
        edit("type_text", text="α\n第二行🙂")
        saved(notes="α\n第二行🙂")
        return ["focused_text", "caret", "selection", "unicode", "multiline",
                "read_only_refusal", "click_without_activation", "targeted_scroll"]
    finally:
        app.terminate()
        app.wait(timeout=5)


def main():
    guest({"action": "start"})
    Path("/var/lib/eval").mkdir(exist_ok=True)
    app = subprocess.Popen(["/usr/bin/python3", str(ROOT / "evals/desktop/apps.py"), "expense"], env=ENV)
    try:
        for _ in range(50):
            if "Expense Report" in request({"action": "read"})["text"]:
                break
            time.sleep(.1)
        else:
            raise AssertionError("The GTK application did not start.")
        old, _ = action("set_value", "text", "Employee name", value="Ada")
        assert not request({"action": "click", "app": "apps.py", "observation": old["observation"],
                            "element_index": target(old, "button", "Submit")})["action_executed"]
        action("set_value", "spin button", "Amount (USD)", value="42.50")
        action("click", "check box", "Reimbursable")
        state = request({"action": "read", "app": "apps.py", "screenshot": True})
        assert len(state["images"]) == 1 and Path(state["images"][0]).is_file()
        # A worker restart must invalidate old indexes without repeating input.
        os.killpg(int(Path("/tmp/moyai-native-desktop/ocu/browser-driver.pid").read_text()), signal.SIGKILL)
        refused = request({"action": "click", "app": "apps.py", "observation": state["observation"],
                           "element_index": target(state, "button", "Submit")})
        assert not refused["action_executed"]
        action("click", "button", "Submit")
        saved = json.loads(Path("/var/lib/eval/expense_submission.json").read_text())
        assert saved == {"name": "Ada", "amount": 42.5, "category": "Meals", "reimbursable": True}, saved
        browser = guest({"action": "browser", "command": {"action": "open", "url": "http://127.0.0.1:4173",
                                                           "new_tab": True}})
        assert browser["navigation_error"]["code"] == "ERR_CONNECTION_REFUSED", browser
        # Compare one identical native result before/after formatting, with all text retained.
        spec = importlib.util.spec_from_file_location("ocu_smoke", ROOT / "native/ocu_guest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ocu = module.Ocu()
        try:
            raw = ocu.state("apps.py")
            rendered = module.content(raw)
            assert rendered["text"] == "\n".join(p["text"] for p in raw["content"] if p["type"] == "text")
            assert not rendered["images"]
            assert any(p["type"] == "image" for p in raw["content"])
            sizes = {"raw_json_bytes": len(json.dumps(raw).encode()),
                     "text_response_bytes": len(json.dumps(rendered).encode())}
        finally:
            ocu.process.terminate()
            ocu.process.wait(timeout=5)
        input_checks = check_native_input()
        print(json.dumps({"passed": ["form_values", "checkbox", "submit", "stale_observation", "worker_restart",
                                     "screenshot", "failed_browser_navigation", "full_text_preserved",
                                     *input_checks], **sizes}))
    finally:
        app.terminate()
        app.wait(timeout=5)


if __name__ == "__main__":
    main()
