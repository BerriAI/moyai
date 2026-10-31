"""Guest lifecycle hooks preserve memory; live AWS proof is recorded separately."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def guest(tmp_path, monkeypatch):
    directory = Path(__file__).parents[1] / "lambda_guest"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("lambda_guest_service_test", directory / "service.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "BOOTSTRAPPED", True)
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")
    monkeypatch.setattr(module.checkpoint, "BASELINE", baseline)
    (tmp_path / "initialized.json").write_text('{"id":"test-launch"}')
    yield module
    sys.modules.pop("checkpoint", None)


def test_resume_retains_processes_and_receipts_but_invalidates_hash_hints(guest):
    process = object()
    guest.PROCESSES["command"] = process
    guest.JOBS["job"] = {"state": "done"}
    guest.CACHE.observed_ns = 123
    guest.lifecycle("resume")
    assert guest.PROCESSES["command"] is process
    assert guest.JOBS["job"] == {"state": "done"}
    assert guest.CACHE.observed_ns != 123


def test_suspend_flushes_files_without_stopping_processes(guest, monkeypatch):
    sync = Mock()
    monkeypatch.setattr(guest.os, "sync", sync, raising=False)
    process = object()
    guest.PROCESSES["command"] = process
    guest.lifecycle("suspend")
    sync.assert_called_once_with()
    assert guest.PROCESSES["command"] is process


def test_resume_requires_original_initialization_and_image_baseline(guest):
    (guest.ROOT / "initialized.json").unlink()
    with pytest.raises(RuntimeError, match="uninitialized"):
        guest.lifecycle("resume")
