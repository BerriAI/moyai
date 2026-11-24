"""Recording lifecycle boundaries; real Linux capture is exercised separately."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("desktop_recording_guest", Path(__file__).parents[1] / "desktop.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)
IDENTITY = "a" * 64


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    monkeypatch.setattr(guest, "RECORDINGS", tmp_path)
    monkeypatch.setattr(guest, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(guest.shutil, "which", lambda _: "/usr/bin/available")
    launched = Mock()
    monkeypatch.setattr(guest, "launch", launched)
    return launched


@pytest.mark.parametrize("identity", ["../escape", "/tmp/video", "", "a" * 63, "A" * 64])
def test_recording_identity_cannot_choose_a_path(recorder, identity):
    with pytest.raises(ValueError, match="identity"):
        guest.start_recording(identity, 5)
    recorder.assert_not_called()


@pytest.mark.parametrize("seconds", [True, 0, 4, 121, 5.5, "45"])
def test_capture_always_has_a_bounded_duration(recorder, seconds):
    with pytest.raises(ValueError, match="duration"):
        guest.start_recording(IDENTITY, seconds)
    recorder.assert_not_called()


def test_start_ack_is_not_ready_and_same_identity_does_not_restart(recorder):
    assert guest.start_recording(IDENTITY, 10)["status"] == "recording"
    assert "path" not in guest.recording_status(IDENTITY)
    assert guest.start_recording(IDENTITY, 10)["status"] == "recording"
    assert recorder.call_count == 1
    assert guest.stop_recording(IDENTITY)["status"] == "stopping"
    assert guest.stop_recording(IDENTITY)["status"] == "stopping"
    recorder.assert_called_once()


def test_snapshot_or_deadline_interruption_never_restarts_capture(recorder, monkeypatch):
    guest.start_recording(IDENTITY, 10)
    state_path = guest.RECORDINGS / IDENTITY / "state.json"
    state = json.loads(state_path.read_text())
    monkeypatch.setattr(guest, "boot_id", lambda: "restored-boot")
    assert guest.recording_status(IDENTITY)["status"] == "interrupted"
    monkeypatch.setattr(guest, "boot_id", lambda: "test-boot")
    state["deadline"] = 0
    state_path.write_text(json.dumps(state))
    assert guest.start_recording(IDENTITY, 10)["status"] == "interrupted"
    assert "path" not in guest.recording_status(IDENTITY)
    recorder.assert_called_once()


def test_missing_encoder_is_explicit_and_does_not_start(recorder, monkeypatch):
    monkeypatch.setattr(guest.shutil, "which", lambda _: None)
    assert guest.start_recording(IDENTITY, 10)["status"] == "unavailable"
    recorder.assert_not_called()


def test_stop_unknown_capture_does_not_create_a_worker_or_directory(recorder):
    assert guest.stop_recording(IDENTITY)["status"] == "missing"
    assert not (guest.RECORDINGS / IDENTITY).exists()
    recorder.assert_not_called()
