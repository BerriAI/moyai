import base64
import importlib.util
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ocu_guest_test", Path(__file__).parents[1] / "ocu_guest.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


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
