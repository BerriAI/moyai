"""Regression coverage for product-path evidence in the DeepSeek integration eval."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TASK = Path(__file__).resolve().parents[1] / "evals/tasks/deepseek_support_live"
spec = importlib.util.spec_from_file_location("deepseek_v2_judge", TASK / "tests/judge.py")
assert spec and spec.loader
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)


def provider_request() -> dict:
    return {
        "schema_version": 2, "model": "deepseek-flash", "path": "/v1/chat/completions",
        "response_id": "fixture-observed-response", "status": 200, "content": "OK",
    }


def route_receipt() -> dict:
    return {
        "schema_version": 1, "source": "litellm.proxy.success_callback",
        "litellm_version": "1.107.0.dev3",
        "litellm_path": "/usr/local/lib/python3.13/site-packages/litellm/__init__.py",
        "call_type": "acompletion", "provider": "deepseek", "model": "deepseek-flash",
        "api_base": "https://api.deepseek.com/v1/chat/completions", "response_id": "fixture-observed-response",
        "response_model": "deepseek/deepseek-flash", "content": "OK", "finish_reason": "stop",
    }


def run_grader(tmp_path, monkeypatch, *, receipts, reply, requests=None):
    paths = {name: tmp_path / f"{name}.json" for name in ("case", "result", "requests", "routes", "output", "reward")}
    paths["case"].write_bytes((TASK / "tests/case.json").read_bytes())
    paths["result"].write_text(json.dumps({
        "posted_reply": reply,
        "tool_trace": [{"name": "credentials_list"}, {"name": "credentials_run"}],
    }))
    for name, entries in (("routes", receipts), ("requests", requests or [provider_request()])):
        paths[name].write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    # Even an overly generous LLM judge cannot override the deterministic gate.
    perfect = {**dict.fromkeys(judge.CRITERIA, 2), "unsupported_claim": False, "reason": "Looks good"}
    monkeypatch.setattr(judge, "OpenAI", lambda **_: SimpleNamespace(responses=SimpleNamespace(
        create=lambda **_: SimpleNamespace(output_text=json.dumps(perfect))
    )))
    monkeypatch.setenv("LITELLM_API_BASE", "http://unused.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "synthetic-judge-test-key")
    monkeypatch.setenv("MOYAI_EVAL_JUDGE_MODEL", "unused-test-model")
    monkeypatch.setattr(sys, "argv", ["judge.py"] + [part for name, path in paths.items() for part in (f"--{name}", str(path))])
    judge.main()
    return json.loads(paths["output"].read_text())


def test_direct_provider_success_fails_even_with_perfect_judge(tmp_path, monkeypatch):
    report = run_grader(tmp_path, monkeypatch, receipts=[], reply="Yes, the provider returned OK.")
    assert not report["passed"]
    assert not report["support_verified"]
    assert "gate:litellm_route" in report["failures"]


def test_honest_inability_does_not_become_verified_support(tmp_path, monkeypatch):
    report = run_grader(tmp_path, monkeypatch, receipts=[], reply="I could only test the provider, so LiteLLM support remains unverified.")
    assert not report["passed"]
    assert not report["integration_evidence"]["support_verified"]


def test_completed_litellm_route_matches_provider_response_and_passes(tmp_path, monkeypatch):
    report = run_grader(tmp_path, monkeypatch, receipts=[route_receipt()], reply="The local LiteLLM integration returned OK against the deterministic provider stub.")
    assert report["passed"]
    assert report["support_verified"]
    assert report["task_version"] == 2


@pytest.mark.parametrize("field,value", [
    ("response_id", "unmatched"), ("litellm_version", "unknown"),
    ("litellm_path", "/workspace/litellm/__init__.py"), ("provider", "openai"),
    ("model", "other-model"), ("content", ""), ("finish_reason", "length"),
    ("call_type", "completion"), ("source", "client-header"),
    ("api_base", "https://elsewhere.invalid/v1"),
])
def test_incomplete_or_mismatched_route_is_not_evidence(field, value):
    receipt = {**route_receipt(), field: value}
    assert not judge.integration_evidence([provider_request()], [receipt], "1.107.0.dev3")["support_verified"]


def test_client_claims_do_not_create_route_receipt():
    request = {**provider_request(), "user_agent": "litellm", "x-litellm-version": "1.107.0.dev3"}
    assert not judge.integration_evidence([request], [], "1.107.0.dev3")["support_verified"]


def test_rejected_provider_response_cannot_pass():
    request = {**provider_request(), "status": 400}
    assert not judge.integration_evidence([request], [route_receipt()], "1.107.0.dev3")["support_verified"]


@pytest.mark.parametrize("field,value", [("concise", 99), ("verified", True), ("unsupported_claim", "false")])
def test_invalid_judge_values_fail_closed(field, value):
    judgment = {**dict.fromkeys(judge.CRITERIA, 2), "unsupported_claim": False, "reason": "OK", field: value}
    with pytest.raises(ValueError):
        judge.parse_judgment(json.dumps(judgment))
