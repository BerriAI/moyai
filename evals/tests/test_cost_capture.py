from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from moyai_harbor.costs import recorded_costs
from openai import OpenAI

JUDGE_PATH = Path(__file__).parents[1] / "tasks/deepseek_support_live/tests/judge.py"
spec = importlib.util.spec_from_file_location("harbor_test_judge", JUDGE_PATH)
assert spec is not None and spec.loader is not None
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)


def ledger(cost=None, **changes):
    return {
        "cost_usd": cost, "cost_status": "actual" if cost is not None else "missing",
        "cost_source": "response_header" if cost is not None else None,
        "status": "completed", "coverage_error": None, **changes,
    }


def test_agent_costs_add_actual_receipts_and_preserve_real_zero():
    usage = recorded_costs([ledger("0.100"), ledger("0.2"), ledger("0")], {"requests": 3})
    assert usage["cost_usd_exact"] == "0.300"
    assert usage["cost_status"] == "complete"
    assert usage["priced_requests"] == 3
    assert recorded_costs([ledger("0")], {"requests": 1})["cost_usd"] == 0.0


def test_missing_receipts_are_unknown_or_partial_not_free():
    assert recorded_costs([], {})["cost_usd"] is None
    assert recorded_costs([ledger()], {"requests": 1})["cost_status"] == "unknown"
    usage = recorded_costs([ledger("0.10"), ledger()], {"requests": 2})
    assert usage["cost_usd"] == 0.1
    assert usage["cost_status"] == "partial"
    assert usage["unpriced_requests"] == 1


@pytest.mark.parametrize("changes", [
    {"cost_status": "estimated"}, {"cost_source": None}, {"cost_usd": "NaN"},
    {"cost_usd": "-1"}, {"cost_usd": "1e100000"},
])
def test_agent_ignores_invalid_or_estimated_costs(changes):
    usage = recorded_costs([ledger("1", **changes)], {"requests": 1})
    assert usage["cost_usd"] is None
    assert usage["cost_status"] == "unknown"


def test_capture_gaps_and_failed_execution_cannot_claim_complete_cost():
    for usage in (
        recorded_costs([ledger("1")], {"requests": 2}),
        recorded_costs([ledger("1", coverage_error="capture_gap")], {"requests": 1}),
        recorded_costs([ledger("1")], {"requests": 1}, execution_complete=False),
    ):
        assert usage["cost_usd"] == 1
        assert usage["cost_status"] == "partial"
    usage = recorded_costs([ledger("1"), ledger(status="not_dispatched")], {"requests": 1})
    assert usage["cost_status"] == "complete"
    assert usage["recorded_requests"] == 1


@pytest.mark.parametrize("value", [None, True, False, "-1", "NaN", "Infinity", "1e100000", "1e-100000", {}])
def test_judge_rejects_invalid_charges(value):
    assert judge.judge_usage({}, {"usage": {"cost": value}})["cost_usd"] is None


def test_judge_preserves_header_and_usage_receipts():
    usage = judge.judge_usage({"x-litellm-response-cost": "0.0123"}, {"usage": {"cost": "9"}})
    assert usage["cost_usd_exact"] == "0.0123"
    assert usage["cost_source"] == "response_header"
    assert judge.judge_usage({}, {"usage": {"cost": 0}})["cost_usd"] == 0
    assert judge.judge_usage({}, {"usage": {"x_litellm_response_cost": ".2"}})["cost_usd"] == .2


def invoke_judge(monkeypatch, tmp_path, *, execution_error="", response_text=None, headers=None, status=200):
    paths = {name: tmp_path / f"{name}.json" for name in ("case", "result", "requests", "output", "reward")}
    paths["case"].write_text(json.dumps({"required": []}))
    paths["result"].write_text(json.dumps({"posted_reply": "A reply", "execution_error": execution_error}))
    monkeypatch.setattr(sys, "argv", ["judge.py", *[
        value for name, path in paths.items() for value in (f"--{name}", str(path))
    ]])
    monkeypatch.setenv("LITELLM_API_BASE", "https://gateway.invalid")
    monkeypatch.setenv("LITELLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("MOYAI_EVAL_JUDGE_MODEL", "judge-model")
    judgment = {name: 2 for name in judge.CRITERIA} | {"unsupported_claim": False}
    body = {
        "id": "resp_test", "object": "response", "created_at": 1234, "model": "judge-model",
        "status": "completed", "error": None,
        "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "annotations": [],
                                 "text": response_text if response_text is not None else json.dumps(judgment)}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers=headers or {})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(judge, "OpenAI", lambda **kwargs: OpenAI(**kwargs, http_client=client))
        judge.main()
    return json.loads(paths["output"].read_text()), calls


def test_real_openai_raw_response_preserves_judge_usage(monkeypatch, tmp_path):
    result, calls = invoke_judge(monkeypatch, tmp_path, headers={"x-litellm-response-cost": "0.25"})
    assert len(calls) == 1
    assert result["passed"] is True
    assert result["judge_usage"]["cost_usd"] == .25
    assert result["judge_usage"]["input_tokens"] == 10


def test_failed_judgment_keeps_billed_cost(monkeypatch, tmp_path):
    result, _ = invoke_judge(monkeypatch, tmp_path, response_text="invalid", headers={"x-litellm-response-cost": ".3"})
    assert result["passed"] is False
    assert result["judge_usage"]["cost_usd"] == .3


def test_failed_request_is_unknown_not_zero(monkeypatch, tmp_path):
    result, calls = invoke_judge(monkeypatch, tmp_path, status=500)
    assert len(calls) == 1
    assert result["passed"] is False
    assert result["judge_usage"]["cost_usd"] is None
    assert result["judge_usage"]["cost_status"] == "unknown"


def test_skipped_judge_has_known_zero_charge(monkeypatch, tmp_path):
    result, calls = invoke_judge(monkeypatch, tmp_path, execution_error="agent failed")
    assert calls == []
    assert result["passed"] is False
    assert result["judge_usage"]["requests"] == 0
    assert result["judge_usage"]["cost_status"] == "complete"
    assert result["judge_usage"]["cost_usd"] == 0
