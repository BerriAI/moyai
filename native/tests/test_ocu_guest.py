import base64
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ocu_guest_test", Path(__file__).parents[1] / "ocu_guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


def test_ocu_child_uses_system_python_without_changing_workspace_or_node(monkeypatch, tmp_path):
    """Exercise the subprocess boundary with a shadowing workspace Python."""
    executables = tmp_path / "bin"
    executables.mkdir()
    wrong_python = executables / "python3"
    wrong_python.write_text("#!/bin/sh\necho 'No module named gi' >&2\nexit 1\n")
    wrong_python.chmod(0o755)
    node = executables / "node"
    node.write_text("#!/bin/sh\necho workspace-node\n")
    node.chmod(0o755)
    ocu_cli = executables / "ocu"
    ocu_cli.write_text(f"""#!{sys.executable}
import json, subprocess, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "tools/list":
        result = {{"tools": [{{"name": "get_app_state", "inputSchema": {{"properties": {{}}}}}}]}}
    elif request["method"] == "tools/call":
        result = {{
            "python": subprocess.check_output(["python3", "-c", "import sys; print(sys.executable)"], text=True).strip(),
            "node": subprocess.check_output(["node"], text=True).strip(),
        }}
    else:
        result = {{}}
    print(json.dumps({{"id": request["id"], "result": result}}), flush=True)
""")
    ocu_cli.chmod(0o755)
    path = str(executables) + os.pathsep + os.environ.get("PATH", os.defpath)
    monkeypatch.setenv("PATH", path)
    monkeypatch.setattr(guest, "SYSTEM_PYTHON", sys.executable)

    assert subprocess.run(["python3", "-c", "import gi"], check=False, capture_output=True).returncode == 1
    ocu = guest.Ocu()
    try:
        result = ocu.call("list_apps", {})
        assert Path(result["python"]).resolve() == Path(sys.executable).resolve()
        assert result["node"] == "workspace-node"
        assert os.environ["PATH"] == path
        assert shutil.which("python3") == str(wrong_python)
        # Later requests still need the private lookup directory.
        assert ocu.call("list_apps", {}) == result
    finally:
        ocu.process.terminate()
        ocu.process.wait(timeout=5)
        ocu.python_bin.cleanup()


class Ocu(guest.Ocu):
    def __init__(self):
        self.observation = None
        self.state_options = {}  # Pinned Linux 1.1.2 has no include_screenshot option.
        self.text = "App=Test\n\t1 button Submit\n\t2 text Name"
        self.image = b"test-image"
        self.calls = []
        self.failure = False

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool != "get_app_state":
            return {"isError": self.failure, "content": [{"type": "text", "text": "done"}]}
        return {"content": [{"type": "text", "text": self.text},
                            {"type": "image", "mimeType": "image/png",
                             "data": base64.b64encode(self.image).decode()}]}


def test_read_preserves_full_text_and_only_returns_requested_media(monkeypatch, tmp_path):
    monkeypatch.setattr(guest, "SCREENSHOTS", tmp_path)
    ocu = Ocu()
    ocu.text += "x" * 20000
    state = ocu.observe("Test", "turn")
    assert state["text"] == ocu.text and not state["images"]
    assert not list(tmp_path.iterdir())
    assert ocu.calls[0][1] == {"app": "Test", "text_limit": "max"}
    state = ocu.observe("Test", "turn", screenshot=True)
    assert Path(state["images"][0]).read_bytes() == b"test-image"


@pytest.mark.parametrize("change", ["text", "context", "restart", "index"])
def test_stale_observations_never_send_input(change):
    ocu = Ocu()
    state = ocu.observe("Test", "turn")
    if change == "text":
        ocu.text += "\n3 button New"
    if change == "restart":
        ocu.observation = None
    result = ocu.perform({"action": "click", "app": "Test", "observation": state["observation"],
                          "element_index": "99" if change == "index" else "1",
                          "context": "new-turn" if change == "context" else "turn"}, time.time() + 10)
    assert result["action_executed"] is False
    assert all(tool == "get_app_state" for tool, _ in ocu.calls)


def test_action_uses_persistent_observed_state_and_only_its_arguments():
    ocu = Ocu()
    state = ocu.observe("Test", "turn")
    result = ocu.perform({"action": "set_value", "app": "Test", "observation": state["observation"],
                          "element_index": "2", "value": "Ada", "pages": 1, "context": "turn"}, time.time() + 10)
    assert result["action_executed"] is True
    assert [(tool, args) for tool, args in ocu.calls if tool != "get_app_state"] == [
        ("set_value", {"app": "Test", "element_index": "2", "value": "Ada"}),
    ]


@pytest.mark.parametrize("change", [False, True])
def test_coordinate_click_requires_same_screenshot_even_when_text_is_unchanged(monkeypatch, tmp_path, change):
    monkeypatch.setattr(guest, "SCREENSHOTS", tmp_path)
    ocu = Ocu()
    state = ocu.observe("Test", "turn", screenshot=True)
    if change:
        ocu.image = b"button-moved-with-unchanged-accessibility-text"
    result = ocu.perform({"action": "click", "app": "Test", "observation": state["observation"],
                          "x": 40, "y": 50, "context": "turn"}, time.time() + 10)
    assert result["action_executed"] is not change
    assert sum(tool == "click" for tool, _ in ocu.calls) == (0 if change else 1)
    assert result["text"] == state["text"]


def test_native_error_after_input_stays_uncertain_and_invalidates_observation():
    ocu = Ocu()
    state = ocu.observe("Test", "turn")
    ocu.failure = True
    with pytest.raises(RuntimeError, match="did not confirm"):
        ocu.perform({"action": "click", "app": "Test", "observation": state["observation"],
                     "element_index": "1", "context": "turn"}, time.time() + 10)
    assert ocu.observation is None
    assert sum(tool == "click" for tool, _ in ocu.calls) == 1
