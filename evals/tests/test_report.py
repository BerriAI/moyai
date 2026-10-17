from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.moyai_harbor.report import (
    CRITERIA,
    build_report,
    costs,
    load_history,
    main,
    summary_text,
    write_report,
)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    jobs, tasks = tmp_path / "jobs", tmp_path / "tasks"
    (tasks / "support").mkdir(parents=True)
    (tasks / "support" / "task.toml").write_text('schema_version = "1.4"\n')
    return jobs, tasks


def make_trial(
    jobs: Path, index: int, *, reward: int = 1, **result_fields: object
) -> Path:
    trial = jobs / "job" / f"support__{index}"
    save_json(
        trial / "result.json",
        {
            "task_name": "support",
            "started_at": "2026-10-17T10:00:00+00:00",
            "finished_at": "2026-10-17T10:01:30+00:00",
            **result_fields,
        },
    )
    save_json(
        trial / "agent" / "moyai_result.json",
        {
            "tokens_cost": {"cost_usd": 0.02, "cost_status": "complete"},
        },
    )
    save_json(
        trial / "verifier" / "judgment.json",
        {
            "judgment": {criterion: 2 for criterion in CRITERIA},
            "judge_usage": {"cost_usd": 0.005, "cost_status": "complete"},
        },
    )
    (trial / "verifier" / "reward.txt").write_text(str(reward))
    return trial


def report_for(inputs: tuple[Path, Path], **kwargs: object) -> dict:
    return build_report(*inputs, run_id="test-run", commit="abc123", **kwargs)


def test_missing_trials_are_failures_not_free_or_fast_measurements(
    inputs: tuple[Path, Path],
) -> None:
    report = report_for(inputs)
    assert report["task_count"] == 1
    assert report["trial_count"] == 3
    assert report["passed_trials"] == 0
    assert not report["all_passed"]
    assert report["cost_usd"] is None
    assert report["known_cost_usd"] is None
    assert report["tasks"][0]["mean_runtime_seconds"] is None
    assert all(
        trial["error"] == "Missing trial" for trial in report["tasks"][0]["trials"]
    )
    assert "N/A" in summary_text(report)
    assert "$0.0000" not in summary_text(report)


def test_task_gate_distinct_from_trial_pass_rate_and_metrics(
    inputs: tuple[Path, Path],
) -> None:
    jobs, _ = inputs
    for index, reward in enumerate((1, 1, 0)):
        make_trial(jobs, index, reward=reward)
    report = report_for(inputs)
    task = report["tasks"][0]
    assert report["all_passed"]
    assert report["task_pass_rate"] == 100
    assert report["trial_pass_rate"] == pytest.approx(200 / 3)
    assert task["mean_score"] == 14
    assert task["score_samples"] == 3
    assert task["mean_runtime_seconds"] == 90
    assert task["cost_usd"] == pytest.approx(0.075)
    assert task["trials"][0]["agent_cost_usd"] == 0.02
    assert task["trials"][0]["judge_cost_usd"] == 0.005
    assert "100% pass (1/1 tasks)" in summary_text(report)
    assert "- support: P (2/3 trials)" in summary_text(report)
    assert "Trial pass rate: 66.7%" in summary_text(report)


@pytest.mark.parametrize(
    "result_fields",
    [
        {"exception_info": {"exception_type": "TimeoutError"}},
        {"finished_at": None},
        {"started_at": None},
        {"started_at": "2026-10-17T10:05:00+00:00"},
    ],
)
def test_reward_does_not_override_exception_or_incomplete_result(
    inputs: tuple[Path, Path],
    result_fields: dict,
) -> None:
    make_trial(inputs[0], 0, **result_fields)
    report = report_for(inputs)
    trial = report["tasks"][0]["trials"][0]
    assert trial["reward"] == 1
    assert not trial["passed"]
    assert trial["error"]


def test_unfinished_directory_and_malformed_result_are_counted(
    inputs: tuple[Path, Path],
) -> None:
    trial = inputs[0] / "job" / "support__interrupted"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text("{broken")
    report = report_for(inputs)
    assert report["tasks"][0]["observed_trials"] == 1
    assert report["tasks"][0]["trials"][0]["error"] == "Incomplete Harbor trial"
    assert report["passed_trials"] == 0


def test_extra_attempts_cannot_silently_change_the_gate(
    inputs: tuple[Path, Path],
) -> None:
    for index in range(4):
        make_trial(inputs[0], index)
    report = report_for(inputs)
    assert not report["all_passed"]
    assert not report["tasks"][0]["passed"]
    assert report["tasks"][0]["error"] == "Expected 3 trials, found 4"


def test_incomplete_cost_keeps_subtotal_but_not_an_exact_total(
    inputs: tuple[Path, Path],
) -> None:
    for index in range(3):
        make_trial(inputs[0], index)
    trial = inputs[0] / "job" / "support__0"
    save_json(
        trial / "agent" / "moyai_result.json",
        {
            "tokens_cost": {"cost_usd": 0.02, "cost_status": "partial"},
        },
    )
    report = report_for(inputs)
    assert report["cost_usd"] is None
    assert report["known_cost_usd"] == pytest.approx(0.075)
    assert report["cost_status"] == "partial"
    assert "N/A ($0.0750 known)" in summary_text(report)


def test_known_zero_is_not_unknown() -> None:
    assert costs([{"cost_usd": 0}, {"cost_usd": 0}])["cost_usd"] == 0
    assert costs([{}, {}])["cost_usd"] is None
    assert (
        costs([{"cost_usd": float("nan")}, {"cost_usd": -1}])["known_cost_usd"] is None
    )
    assert costs([{"cost_usd": 10**1000}])["cost_usd"] is None


def test_malformed_nested_results_produce_unknown_metrics(
    inputs: tuple[Path, Path],
) -> None:
    trial = make_trial(inputs[0], 0, task_name=[])
    save_json(trial / "agent" / "moyai_result.json", {"tokens_cost": [1]})
    save_json(trial / "verifier" / "judgment.json", {"judge_usage": [1]})
    row = report_for(inputs)["tasks"][0]["trials"][0]
    assert row["cost_usd"] is None
    assert row["known_cost_usd"] is None


def test_missing_rubric_criteria_and_bad_times_are_not_zero(
    inputs: tuple[Path, Path],
) -> None:
    trial = make_trial(inputs[0], 0, started_at="not a date")
    save_json(trial / "verifier" / "judgment.json", {"judgment": {"correct": 2}})
    row = report_for(inputs)["tasks"][0]["trials"][0]
    assert row["score"] is None
    assert row["score_max"] == 14
    assert row["runtime_seconds"] is None
    assert row["cost_usd"] is None
    assert row["known_cost_usd"] == 0.02


def test_empty_task_set_and_run_errors_fail(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    empty = build_report(inputs[0], tmp_path / "absent", run_id="empty", commit="abc")
    assert not empty["all_passed"]
    assert empty["run_error"] == "No task.toml files found"
    for index in range(3):
        make_trial(inputs[0], index)
    report = report_for(inputs, run_error="Harbor process exited 1")
    assert not report["all_passed"]
    assert summary_text(report).startswith("Harbor run failed")


def test_history_is_bounded_and_current_run_is_not_duplicated(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    history_dir = tmp_path / "history"
    for index in range(30):
        previous = report_for(inputs)
        previous["run_id"] = f"old-{index}"
        previous["generated_at"] = (
            f"2026-10-16T{index // 2:02}:{30 * (index % 2):02}:00+00:00"
        )
        save_json(history_dir / f"{index}.json", previous)
    current = report_for(inputs)
    save_json(history_dir / "current.json", current)
    (history_dir / "bad.json").write_text("{broken")
    malformed = dict(current, run_id="bad-tasks", tasks=[None])
    save_json(history_dir / "bad-tasks.json", malformed)
    malformed = dict(
        current, run_id="bad-metrics", tasks=[{"name": "support", "cost_usd": "free"}]
    )
    save_json(history_dir / "bad-metrics.json", malformed)
    history = load_history(history_dir, current)
    assert len(history) == 24
    assert history[0]["run_id"] == "old-7"
    assert history[-1]["run_id"] == "test-run"
    assert sum(row["run_id"] == "test-run" for row in history) == 1


def test_pdf_json_and_summary_are_generated_even_for_failed_run(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    report = report_for(inputs)
    output, history_dir = tmp_path / "output", tmp_path / "history"
    write_report(report, output, history_dir)
    assert json.loads((output / "report.json").read_text())["all_passed"] is False
    assert (output / "summary.txt").read_text().startswith("0% pass")
    pdf = (output / "report.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-")
    assert pdf.endswith(b"%%EOF\n")
    assert len(list(history_dir.glob("*.json"))) == 1


def test_history_cleanup_preserves_unrelated_json(
    inputs: tuple[Path, Path], tmp_path: Path
) -> None:
    report = report_for(inputs)
    history_dir = tmp_path / "history"
    for index in range(30):
        run_id = f"old-{index}"
        key = hashlib.sha256(run_id.encode()).hexdigest()[:24]
        save_json(history_dir / f"{key}.json", dict(report, run_id=run_id))
    notes = history_dir / "notes.json"
    foreign = history_dir / f"{'a' * 24}.json"
    save_json(notes, dict(report, run_id="notes"))
    save_json(foreign, {"schema_version": 2, "unrelated": True})
    write_report(report, tmp_path / "output", history_dir)
    assert notes.exists()
    assert foreign.exists()
    assert len(list(history_dir.glob("*.json"))) == 26


def test_cli_generates_failed_report_without_exiting_before_notification(
    inputs: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "cli"
    monkeypatch.setattr(
        "sys.argv",
        [
            "report",
            "--jobs-root",
            str(inputs[0]),
            "--tasks-root",
            str(inputs[1]),
            "--output-dir",
            str(output),
            "--run-id",
            "cli",
            "--commit",
            "abc123",
        ],
    )
    main()
    assert (output / "report.pdf").exists()
    assert json.loads((output / "report.json").read_text())["all_passed"] is False


@pytest.mark.parametrize("attempts,required_passes", [(0, 0), (3, 4), (3, 0)])
def test_invalid_gate_is_rejected(
    inputs: tuple[Path, Path], attempts: int, required_passes: int
) -> None:
    with pytest.raises(ValueError, match="required passes"):
        report_for(inputs, attempts=attempts, required_passes=required_passes)
