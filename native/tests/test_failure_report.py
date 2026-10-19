import json

from moyai.failure_report import FailureReport
from moyai.models import Turn


async def test_failure_report_falls_back_when_trace_export_fails(runtime, active, monkeypatch):
    _, turn, _ = active
    error = "Codex stopped (usageLimitExceeded)"
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Turn, turn.id)
        saved.status = "failed"
        saved.error = error
    turn.status = "failed"
    turn.error = error

    async def fail_export(*_args):
        raise RuntimeError("trace store unavailable")

    monkeypatch.setattr("moyai.failure_report.debug_trace", fail_export)
    report = await FailureReport.for_turn(runtime, turn)

    assert report.text == f"This task stopped before it completed: {error}"
    assert report.debug_log.filename == f"moyai-debug-{turn.id}.json"
    assert report.debug_log.title == "Moyai debug log"
    assert json.loads(report.debug_log.content) == {
        "session_id": turn.session_id,
        "turn_id": turn.id,
        "error": error,
        "trace_export_error": "trace store unavailable",
    }
