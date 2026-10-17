"""Summarize one isolated Harbor run without importing Harbor or production code."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any
from xml.sax.saxutils import escape

CRITERIA = (
    "verified",
    "verdict_first",
    "correct",
    "context",
    "value_add",
    "action",
    "concise",
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (AttributeError, TypeError, ValueError):
        return None


def costs(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep incomplete cost coverage distinct from a measured zero-dollar run."""
    parts = [mapping(part) for part in parts]
    values = [number(part.get("cost_usd")) for part in parts]
    known = [number(part.get("known_cost_usd", part.get("cost_usd"))) for part in parts]
    complete = bool(parts) and all(
        value is not None and part.get("cost_status", "complete") == "complete"
        for part, value in zip(parts, values, strict=True)
    )
    known_values = [value for value in known if value is not None]
    subtotal = sum(known_values) if known_values else None
    return {
        "cost_usd": subtotal if complete else None,
        "known_cost_usd": subtotal,
        "cost_status": "complete"
        if complete
        else "partial"
        if known_values
        else "unknown",
    }


def trial_report(path: Path | None, index: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": path.name if path else f"Trial {index + 1} (missing)",
        "passed": False,
        "reward": None,
        "score": None,
        "score_max": None,
        "runtime_seconds": None,
        "cost_usd": None,
        "known_cost_usd": None,
        "cost_status": "unknown",
        "error": "Missing trial",
    }
    if path is None:
        return row
    result = read_json(path / "result.json")
    agent = read_json(path / "agent" / "moyai_result.json")
    judgment = read_json(path / "verifier" / "judgment.json")
    error = ""
    started, finished = (
        timestamp(result.get("started_at")),
        timestamp(result.get("finished_at")),
    )
    if started and finished and finished >= started:
        row["runtime_seconds"] = (finished - started).total_seconds()
    if not result or not started or not finished or finished < started:
        error = "Incomplete Harbor trial"
    if result.get("exception_info"):
        error = "Harbor trial exception"
    if agent.get("execution_error"):
        error = "Agent execution failed"
    if judgment.get("judge_error"):
        error = "Judge execution failed"
    try:
        reward = float((path / "verifier" / "reward.txt").read_text().strip())
        row["reward"] = number(reward)
    except (OSError, ValueError):
        error = error or "Missing or invalid reward"
    if row["reward"] is None:
        error = error or "Invalid reward"
    rubric = judgment.get("judgment")
    if isinstance(rubric, dict):
        row["score_max"] = 14
        values = [number(rubric.get(key)) for key in CRITERIA]
        if all(value in (0, 1, 2) for value in values):
            row["score"] = sum(values)
    else:
        row["score"], row["score_max"] = row["reward"], 1
    agent_usage = mapping(agent.get("tokens_cost")) or mapping(
        result.get("agent_result")
    )
    judge_usage = mapping(judgment.get("judge_usage"))
    row.update(costs([agent_usage, judge_usage]))
    row["agent_cost_usd"] = number(agent_usage.get("cost_usd"))
    row["judge_cost_usd"] = number(judge_usage.get("cost_usd"))
    row["error"] = error
    row["passed"] = row["reward"] == 1 and not error
    return row


def build_report(
    jobs_root: Path,
    tasks_root: Path,
    *,
    attempts: int = 3,
    required_passes: int = 2,
    run_id: str,
    commit: str,
    run_url: str = "",
    run_error: str = "",
) -> dict[str, Any]:
    if not 1 <= required_passes <= attempts:
        raise ValueError("Expected 1 <= required passes <= attempts")
    task_paths = sorted(path.parent for path in tasks_root.rglob("task.toml"))
    names = [path.name for path in task_paths]
    if len(names) != len(set(names)):
        raise ValueError("Task directory names must be unique")
    candidates: dict[str, list[Path]] = {name: [] for name in names}
    # A runner must provide a fresh jobs_root for each invocation. Walking trial
    # directories also finds interrupted attempts without a result.json yet.
    for path in sorted(jobs_root.rglob("*")):
        if not path.is_dir() or "__" not in path.name:
            continue
        result = read_json(path / "result.json")
        name = result.get("task_name")
        if not isinstance(name, str) or name not in candidates:
            prefix = path.name.split("__", 1)[0]
            matches = [name for name in names if name[:32].rstrip("_-") == prefix]
            name = matches[0] if len(matches) == 1 else None
        if name in candidates:
            candidates[name].append(path)
    tasks = []
    for name in names:
        paths = candidates[name]
        trials = [
            trial_report(paths[i] if i < len(paths) else None, i)
            for i in range(attempts)
        ]
        passed = sum(trial["passed"] for trial in trials)
        scores = [trial["score"] for trial in trials if trial["score"] is not None]
        runtimes = [
            trial["runtime_seconds"]
            for trial in trials
            if trial["runtime_seconds"] is not None
        ]
        task = {
            "name": name,
            "passed": passed >= required_passes and len(paths) <= attempts,
            "passed_trials": passed,
            "expected_trials": attempts,
            "observed_trials": len(paths),
            "error": f"Expected {attempts} trials, found {len(paths)}"
            if len(paths) > attempts
            else "",
            "mean_score": mean(scores) if scores else None,
            "score_samples": len(scores),
            "mean_runtime_seconds": mean(runtimes) if runtimes else None,
            "runtime_samples": len(runtimes),
            "trials": trials,
            **costs(trials),
        }
        tasks.append(task)
    passed_tasks = sum(task["passed"] for task in tasks)
    trial_count = len(tasks) * attempts
    passed_trials = sum(task["passed_trials"] for task in tasks)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "commit": commit,
        "run_url": run_url,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_error": run_error or ("No task.toml files found" if not tasks else ""),
        "attempts": attempts,
        "required_passes": required_passes,
        "task_count": len(tasks),
        "passed_tasks": passed_tasks,
        "task_pass_rate": 100 * passed_tasks / len(tasks) if tasks else 0,
        "trial_count": trial_count,
        "passed_trials": passed_trials,
        "trial_pass_rate": 100 * passed_trials / trial_count if trial_count else 0,
        "all_passed": bool(tasks) and passed_tasks == len(tasks) and not run_error,
        "tasks": tasks,
        **costs(tasks),
    }


def money(row: dict[str, Any]) -> str:
    if row.get("cost_usd") is not None:
        return f"${row['cost_usd']:.4f}"
    if row.get("known_cost_usd") is not None:
        return f"N/A (${row['known_cost_usd']:.4f} known)"
    return "N/A"


def percent(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def summary_text(report: dict[str, Any]) -> str:
    headline = (
        f"{percent(report['task_pass_rate'])}% pass "
        f"({report['passed_tasks']}/{report['task_count']} tasks)"
    )
    if report["run_error"]:
        headline = f"Harbor run failed - {headline}"
    lines = [headline, ""]
    lines.extend(
        f"- {task['name']}: {'P' if task['passed'] else 'F'} "
        f"({task['passed_trials']}/{task['expected_trials']} trials)"
        for task in report["tasks"]
    )
    lines.extend(
        [
            "",
            f"Task gate: at least {report['required_passes']}/{report['attempts']} trials pass.",
            (
                f"Trial pass rate: {percent(report['trial_pass_rate'])}% "
                f"({report['passed_trials']}/{report['trial_count']})."
            ),
            f"Agent + judge LLM cost: {money(report)}; infrastructure excluded.",
            f"Run: {report['run_id']} | Commit: {report['commit'][:12]}",
        ]
    )
    if report["run_error"]:
        lines.append(f"Run error: {report['run_error']}")
    if report["run_url"]:
        lines.append(report["run_url"])
    return "\n".join(lines) + "\n"


def load_history(
    history_dir: Path | None, report: dict[str, Any]
) -> list[dict[str, Any]]:
    runs = []
    if history_dir and history_dir.exists():
        for path in history_dir.glob("*.json"):
            previous = read_json(path)
            if (
                previous.get("schema_version") == 1
                and previous.get("run_id") != report["run_id"]
                and isinstance(previous.get("tasks"), list)
                and timestamp(previous.get("generated_at"))
                and all(
                    isinstance(task, dict)
                    and isinstance(task.get("name"), str)
                    and all(
                        task.get(metric) is None or number(task.get(metric)) is not None
                        for metric in ("mean_score", "mean_runtime_seconds", "cost_usd")
                    )
                    for task in previous["tasks"]
                )
            ):
                runs.append(previous)
    runs.sort(key=lambda run: timestamp(run["generated_at"]))
    return [*runs[-23:], report]


def chart(
    title: str,
    values: list[float | None],
    labels: list[str],
    *,
    trend: bool = False,
    width: int = 235,
    height: int = 155,
) -> Any:
    """Small vector chart; absent measurements are gaps and explicitly labeled N/A."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String
    from reportlab.lib import colors

    drawing = Drawing(width, height)
    drawing.add(
        String(
            0,
            height - 12,
            title,
            fontName="Helvetica-Bold",
            fontSize=10,
            fillColor=colors.HexColor("#172337"),
        )
    )
    left, bottom, plot_width, plot_height = 35, 25, width - 43, height - 56
    valid = [value for value in values if value is not None]
    maximum = max(valid, default=1) or 1
    maximum *= 1.15
    for fraction in (0, 0.5, 1):
        y = bottom + plot_height * fraction
        drawing.add(
            Line(left, y, left + plot_width, y, strokeColor=colors.HexColor("#e2e8f0"))
        )
        label = f"{maximum * fraction:.3g}"
        drawing.add(
            String(
                left - 5,
                y - 3,
                label,
                textAnchor="end",
                fontSize=7,
                fillColor=colors.HexColor("#64748b"),
            )
        )
    spacing = plot_width / max(len(values), 1)
    previous = None
    for index, (value, label) in enumerate(zip(values, labels, strict=True)):
        x = left + spacing * (index + 0.5)
        if not trend or index in (0, len(values) - 1):
            drawing.add(
                String(
                    x,
                    10,
                    label,
                    textAnchor="middle",
                    fontSize=7,
                    fillColor=colors.HexColor("#64748b"),
                )
            )
        if value is None:
            if not trend or len(values) <= 6:
                drawing.add(
                    String(
                        x,
                        bottom + 5,
                        "N/A",
                        textAnchor="middle",
                        fontSize=7,
                        fillColor=colors.HexColor("#64748b"),
                    )
                )
            previous = None
            continue
        y = bottom + plot_height * value / maximum
        if trend:
            if previous:
                drawing.add(
                    Line(
                        *previous,
                        x,
                        y,
                        strokeColor=colors.HexColor("#2563eb"),
                        strokeWidth=1.5,
                    )
                )
            drawing.add(
                Rect(
                    x - 2,
                    y - 2,
                    4,
                    4,
                    fillColor=colors.HexColor("#2563eb"),
                    strokeColor=None,
                )
            )
            previous = (x, y)
        else:
            drawing.add(
                Rect(
                    x - spacing * 0.27,
                    bottom,
                    spacing * 0.54,
                    y - bottom,
                    fillColor=colors.HexColor("#2563eb"),
                    strokeColor=None,
                )
            )
            drawing.add(
                String(
                    x,
                    y + 5,
                    f"{value:.4g}",
                    textAnchor="middle",
                    fontSize=8,
                    fillColor=colors.HexColor("#172337"),
                )
            )
    if not valid:
        drawing.add(
            String(
                left + plot_width / 2,
                height / 2,
                "No measured data",
                textAnchor="middle",
                fontSize=9,
                fillColor=colors.HexColor("#64748b"),
            )
        )
    return drawing


def write_pdf(
    report: dict[str, Any], history: list[dict[str, Any]], path: Path
) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "Small",
            parent=styles["Normal"],
            fontSize=8,
            leading=12,
            textColor=colors.HexColor("#475569"),
        )
    )
    styles["Title"].textColor = colors.HexColor("#172337")
    styles["Title"].alignment = 0
    styles["Heading2"].textColor = colors.HexColor("#172337")
    document = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        rightMargin=42,
        leftMargin=42,
        topMargin=36,
        bottomMargin=34,
    )
    story: list[Any] = []

    def paragraph(text: str, style: str = "Normal") -> Any:
        return Paragraph(escape(text), styles[style])

    def table(rows: list[list[Any]], widths: list[float]) -> Any:
        result = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f8")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#172337")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                ]
            )
        )
        return result

    story.extend(
        [
            paragraph(
                "SYNTHETIC PREVIEW - test data"
                if report.get("synthetic_preview")
                else "Harbor evaluation",
                "Title",
            ),
            paragraph(
                f"{percent(report['task_pass_rate'])}% of tasks pass", "Heading1"
            ),
            paragraph(
                f"{report['passed_tasks']}/{report['task_count']} tasks | {report['passed_trials']}/{report['trial_count']} trials ({percent(report['trial_pass_rate'])}%) | LLM cost {money(report)}"
            ),
            Spacer(1, 12),
            paragraph(
                f"Generated {report['generated_at']} | Run {report['run_id']}", "Small"
            ),
            paragraph(f"Source commit: {report['commit']}", "Small"),
        ]
    )
    if report["run_url"]:
        story.append(paragraph(f"Run URL: {report['run_url']}", "Small"))
    if report["run_error"]:
        story.append(paragraph(f"RUN FAILED: {report['run_error']}", "Heading2"))
    story.extend(
        [
            Spacer(1, 16),
            table(
                [
                    ["Task", "Result", "Trials pass", "Avg time (s)", "LLM cost (USD)"],
                    *[
                        [
                            paragraph(task["name"], "Small"),
                            "PASS" if task["passed"] else "FAIL",
                            f"{task['passed_trials']}/{task['expected_trials']}",
                            f"{task['mean_runtime_seconds']:.1f}"
                            if task["mean_runtime_seconds"] is not None
                            else "N/A",
                            paragraph(money(task), "Small"),
                        ]
                        for task in report["tasks"]
                    ],
                ],
                [280, 65, 80, 105, 145],
            ),
            Spacer(1, 16),
            paragraph(
                f"A task passes when at least {report['required_passes']} of {report['attempts']} attempts pass. Missing and incomplete attempts count as failures. The task pass rate and trial pass rate are distinct.",
                "Small",
            ),
            paragraph(
                "Time is end-to-end Harbor trial duration, including setup and verification. Cost is measured agent + judge LLM usage; infrastructure is excluded. Incomplete pricing is N/A with any known subtotal shown. N/A is never plotted as zero. Score averages use measured samples only.",
                "Small",
            ),
        ]
    )
    for task in report["tasks"]:
        story.append(PageBreak())
        story.append(paragraph(task["name"], "Title"))
        story.append(
            paragraph(
                f"{'PASS' if task['passed'] else 'FAIL'} | {task['passed_trials']}/{task['expected_trials']} trials pass | LLM cost {money(task)}",
                "Heading2",
            )
        )
        trials = task["trials"]
        rubric = any(trial["score_max"] == 14 for trial in trials)
        score_title = "Rubric score (0-14)" if rubric else "Verifier reward (0-1)"
        story.append(paragraph("Current run - one bar per attempt", "Small"))
        labels = [str(index + 1) for index in range(len(trials))]
        story.append(
            Table(
                [
                    [
                        chart(
                            score_title, [trial["score"] for trial in trials], labels
                        ),
                        chart(
                            "Trial wall time (seconds)",
                            [trial["runtime_seconds"] for trial in trials],
                            labels,
                        ),
                        chart(
                            "Agent + judge cost (USD)",
                            [trial["cost_usd"] for trial in trials],
                            labels,
                        ),
                    ]
                ],
                colWidths=[252, 252, 252],
            )
        )
        rows = [["Attempt", "Result", "Notes"]]
        rows.extend(
            [
                [
                    str(index + 1),
                    "PASS" if trial["passed"] else "FAIL",
                    paragraph(
                        trial["error"]
                        or (
                            "Verifier returned a failing reward"
                            if not trial["passed"]
                            else "Complete"
                        ),
                        "Small",
                    ),
                ]
                for index, trial in enumerate(trials)
            ]
        )
        story.append(table(rows, [65, 65, 626]))
        if task["error"]:
            story.append(paragraph(task["error"], "Small"))
        story.append(Spacer(1, 9))
        story.append(
            paragraph(
                "N/A indicates an unmeasured value. Time includes setup and verification. Cost includes agent and judge LLM calls; infrastructure is excluded.",
                "Small",
            )
        )
        if len(history) == 1:
            story.append(
                paragraph(
                    "No earlier runs are available yet. Future reports will add historical score, time, and cost trends.",
                    "Small",
                )
            )
            continue
        story.append(PageBreak())
        story.append(paragraph(f"{task['name']} - history", "Title"))
        story.append(
            paragraph(
                f"Recent trend - last {len(history)} run(s); left to right oldest to newest; gaps mean missing measurements",
                "Small",
            )
        )
        historical_tasks = [
            next((item for item in run["tasks"] if item["name"] == task["name"]), {})
            for run in history
        ]
        history_labels = [
            timestamp(run["generated_at"]).strftime("%m/%d %H:%M") for run in history
        ]
        for title, metric in (
            ("Mean score", "mean_score"),
            ("Mean trial wall time (seconds)", "mean_runtime_seconds"),
            ("Total task LLM cost (USD)", "cost_usd"),
        ):
            story.append(
                chart(
                    title,
                    [item.get(metric) for item in historical_tasks],
                    history_labels,
                    trend=True,
                    width=740,
                    height=137,
                )
            )

    def footer(canvas: Any, doc: Any) -> None:
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        label = (
            "SYNTHETIC PREVIEW - test data"
            if report.get("synthetic_preview")
            else "Actual measurements only"
        )
        canvas.drawString(42, 19, f"Harbor evaluation | All times UTC | {label}")
        canvas.drawRightString(800, 19, str(doc.page))

    document.build(story, onFirstPage=footer, onLaterPages=footer)


def write_report(
    report: dict[str, Any], output_dir: Path, history_dir: Path | None = None
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    history = load_history(history_dir, report)
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    (output_dir / "report.json").write_text(encoded)
    (output_dir / "summary.txt").write_text(summary_text(report))
    write_pdf(report, history, output_dir / "report.pdf")
    if history_dir:
        history_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(report["run_id"].encode()).hexdigest()[:24]
        (history_dir / f"{key}.json").write_text(encoded)
        previous = sorted(
            (
                path
                for path in history_dir.glob("*.json")
                if re.fullmatch(r"[0-9a-f]{24}\.json", path.name)
                and read_json(path).get("schema_version") == 1
            ),
            key=lambda path: path.stat().st_mtime,
        )
        for path in previous[:-24]:
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("jobs-root", "tasks-root", "output-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--history-dir", type=Path)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--required-passes", type=int, default=2)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--run-error", default="")
    args = parser.parse_args()
    report = build_report(
        args.jobs_root,
        args.tasks_root,
        attempts=args.attempts,
        required_passes=args.required_passes,
        run_id=args.run_id,
        commit=args.commit,
        run_url=args.run_url,
        run_error=args.run_error,
    )
    write_report(report, args.output_dir, args.history_dir)
    print(summary_text(report), end="")


if __name__ == "__main__":
    main()
