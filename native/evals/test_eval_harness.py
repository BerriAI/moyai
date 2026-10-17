from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_slack_agent_evals import CASES, CRITERIA, gate_results, parse_judgment, trial_pass, write_summary


def test_parse_judgment_accepts_fenced_json() -> None:
    assert parse_judgment('```json\n{"verified": 2}\n```') == {"verified": 2}


def test_trial_pass_requires_required_criteria_and_minimum_score() -> None:
    case = {"required": ["verified", "correct"]}
    passing = {criterion: 2 for criterion in CRITERIA} | {"unsupported_claim": False}
    assert trial_pass(case, {"deterministic": True}, passing) == (True, [])

    wrong_required_criterion = {**passing, "correct": 1}
    assert trial_pass(case, {"deterministic": True}, wrong_required_criterion) == (
        False,
        ["criterion:correct"],
    )

    low_score = {criterion: 0 for criterion in CRITERIA} | {
        "verified": 2, "unsupported_claim": False,
    }
    assert trial_pass({"required": ["verified"]}, {"deterministic": True}, low_score) == (
        False,
        ["criterion:total_score"],
    )


def test_missing_credential_gate_rejects_requests_to_paste_keys() -> None:
    case = next(item for item in CASES if item["id"] == "missing_credential")
    gates = gate_results(
        case,
        [{"name": "credentials_list"}],
        [],
        "Please paste your API key in this Slack thread.",
    )
    assert gates["credentials_list"]
    assert not gates["no_paste_request"]


def test_step_summary_renders_one_details_block_per_trial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    case = CASES[0]
    report: dict[str, Any] = {
        "repeats": 1,
        "case_pass_counts": {item["id"]: 0 for item in CASES},
        "trials": [{
            "case": case["id"],
            "trial": 1,
            "passed": False,
            "slack_thread_input": [{"text": "input"}],
            "prompt_received": "turn prompt",
            "attached_image_names": ["screenshot.png"],
            "posted_reply": "reply",
            "judgment": {"reason": "not verified"},
            "failures": ["criterion:verified"],
            "judge_raw_json": json.dumps({"reason": "not verified"}),
            "judge_error": "",
        }],
    }

    write_summary(report)

    rendered = summary.read_text()
    assert "| Case | Passes | Trials |" in rendered
    assert rendered.count("<details>") == 1
    assert "screenshot.png" in rendered
    assert "not verified" in rendered
