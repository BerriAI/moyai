"""Protect the evidence gates from plausible-looking but unverified answers."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verification_eval as evaluation


@pytest.mark.parametrize("url", ["https://:synthetic-secret@example.test/v1", "https://user@example.test/v1",
                                "https://example.test/v1?key=synthetic", "http://example.test/v1"])
def test_gateway_credentials_cannot_enter_report(url):
    with pytest.raises(ValueError):
        evaluation.validated_endpoint(url)


@pytest.fixture
def case():
    return {
        "id": "example", "family": "supported", "prompt": "Does this integration work?",
        "evidence": {"access": {"credential": "available"}},
        "probe": {"model": "provider/example", "integration": "litellm", "max_output_tokens": 32,
                  "result": {"status": "success", "model": "example"}},
        "expect": {"probe": "required", "required_reads": ["access"], "diagnosis": "Works in fixture."},
    }


def event(case, name, arguments):
    return {"id": "event-1", "name": name, "arguments": arguments,
            "result": evaluation.dispatch(case, name, arguments)}


def probe_args(**overrides):
    return {"model": "provider/example", "integration": "litellm", "max_output_tokens": 32, **overrides}


def test_documentation_only_cannot_pass_required_probe(case):
    events = [event(case, "read_evidence", {"source": "access"})]
    assert not evaluation.trace_gates(case, events)["appropriate_probe"]


def test_wrong_integration_and_unbounded_probe_do_not_count(case):
    wrong = event(case, "run_smoke_test", probe_args(integration="moyai"))
    excessive = event(case, "run_smoke_test", probe_args(max_output_tokens=10000))
    gates = evaluation.trace_gates(case, [wrong, excessive])
    assert not gates["appropriate_probe"]
    assert not gates["probe_budget"]


def test_failed_probe_is_still_empirical_evidence(case):
    case["probe"]["result"] = {"status": "error", "code": "AuthenticationError"}
    events = [event(case, "read_evidence", {"source": "access"}),
              event(case, "run_smoke_test", probe_args())]
    assert all(evaluation.trace_gates(case, events).values())


def test_prohibited_attempt_fails_even_when_tool_refuses(case):
    case["expect"]["probe"] = "forbidden"
    case["probe"]["result"] = {"status": "blocked"}
    assert not evaluation.trace_gates(case, [event(case, "run_smoke_test", probe_args())])["appropriate_probe"]


def test_malformed_judge_does_not_default_to_pass():
    with pytest.raises(ValueError):
        evaluation.validated_judgment('{"scope": true, "total": 10}')


def test_trial_cannot_pass_on_polished_answer_without_tool_evidence(case, monkeypatch):
    judge = {"scope": 2, "evidence": 3, "diagnosis": 2, "honesty": 2, "clarity": 1,
             "unsupported_execution_claim": False, "reason": "Looks good"}
    replies = iter(["Works!", json.dumps(judge)])

    def complete(*args):
        return {"choices": [{"finish_reason": "stop", "message": {"content": next(replies)}}]}, {}

    monkeypatch.setattr(evaluation, "complete", complete)
    args = SimpleNamespace(max_rounds=2, model="candidate", judge_model="judge",
                           max_completion_tokens=1000, base_url="https://example.test/v1")
    result = evaluation.trial(case, evaluation.configurations("high")[0], 0, args, "Instructions", "test-key")
    assert result["judgment"]["total"] == 10
    assert result["passed"] is False
    assert result["error"] is None


def test_judge_errors_are_inconclusive(case, monkeypatch):
    replies = iter(["Done", "not JSON"])

    def complete(*args):
        return {"choices": [{"finish_reason": "stop", "message": {"content": next(replies)}}]}, {}

    monkeypatch.setattr(evaluation, "complete", complete)
    args = SimpleNamespace(max_rounds=2, model="candidate", judge_model="judge",
                           max_completion_tokens=1000, base_url="https://example.test/v1")
    result = evaluation.trial(case, evaluation.configurations("high")[0], 0, args, "Instructions", "test-key")
    assert result["passed"] is False
    assert result["error"] is not None


@pytest.mark.parametrize(("diagnosis", "unsupported", "passed"), [(2, False, True), (0, False, False), (2, True, False)])
def test_semantic_gates_still_apply_after_a_real_tool_sequence(case, monkeypatch, diagnosis, unsupported, passed):
    calls = [
        {"id": "read", "type": "function", "function": {
            "name": "read_evidence", "arguments": json.dumps({"source": "access"})}},
        {"id": "probe", "type": "function", "function": {
            "name": "run_smoke_test", "arguments": json.dumps(probe_args())}},
    ]
    judge = {"scope": 2, "evidence": 3, "diagnosis": diagnosis, "honesty": 2, "clarity": 1,
             "unsupported_execution_claim": unsupported, "reason": "event-2 contains the fixture result"}
    replies = iter([
        {"finish_reason": "tool_calls", "message": {"content": "Checking.", "tool_calls": calls}},
        {"finish_reason": "stop", "message": {"content": "The simulated route succeeded."}},
        {"finish_reason": "stop", "message": {"content": json.dumps(judge)}},
    ])
    monkeypatch.setattr(evaluation, "complete", lambda *args: ({"choices": [next(replies)]}, {}))
    args = SimpleNamespace(max_rounds=3, model="candidate", judge_model="judge",
                           max_completion_tokens=1000, base_url="https://example.test/v1")
    result = evaluation.trial(case, evaluation.configurations("high")[0], 0, args, "Instructions", "test-key")
    assert all(result["gates"].values())
    assert result["passed"] is passed


def test_budget_is_enforced_before_third_probe_dispatch(case, monkeypatch):
    calls = [{"id": str(i), "type": "function", "function": {
        "name": "run_smoke_test", "arguments": json.dumps(probe_args())}} for i in range(3)]
    monkeypatch.setattr(evaluation, "complete", lambda *args: ({"choices": [{
        "finish_reason": "tool_calls", "message": {"content": None, "tool_calls": calls}}]}, {}))
    dispatched = []
    original = evaluation.dispatch

    def dispatch(*args):
        dispatched.append(args)
        return original(*args)

    monkeypatch.setattr(evaluation, "dispatch", dispatch)
    args = SimpleNamespace(max_rounds=2, model="candidate", judge_model="judge",
                           max_completion_tokens=1000, base_url="https://example.test/v1")
    result = evaluation.trial(case, evaluation.configurations("high")[0], 0, args, "Instructions", "test-key")
    assert len(dispatched) == 2
    assert result["error"] == "Probe attempt budget exhausted"
    assert result["passed"] is False


def test_missing_cost_is_unknown_not_free():
    result = {"config": "baseline", "family": "x", "passed": False, "error": None,
              "events": [], "requests": [{"cost_usd": None}], "agent_seconds": 1}
    summary = evaluation.summarize([result])["baseline"]
    assert summary["agent_cost_usd"] is None
    assert summary["cost_coverage"] == 0


def test_fixture_expectations_and_tool_routes_are_consistent():
    suite = json.loads((Path(evaluation.ROOT) / "evals/verification_cases.json").read_text())
    cases = suite["cases"]
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["split"] for case in cases} == {"development", "holdout"}
    for case in cases:
        assert set(case["expect"]["required_reads"]) <= set(case["evidence"])
        args = {key: case["probe"][key] for key in ("model", "integration", "max_output_tokens")}
        assert evaluation.dispatch(case, "run_smoke_test", args)["result"] == case["probe"]["result"]
        assert case["expect"]["probe"] in {"required", "forbidden"}
