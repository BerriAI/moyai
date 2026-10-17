from __future__ import annotations

import argparse
import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from moyai_harbor import run_report as runner


@pytest.fixture
def arguments(tmp_path: Path) -> argparse.Namespace:
    tasks = tmp_path / "tasks"
    (tasks / "support").mkdir(parents=True)
    (tasks / "support/task.toml").write_text('schema_version = "1.4"\n')
    return argparse.Namespace(env_file=None, output_root=tmp_path / "reports", tasks_root=tasks,
                              timeout_seconds=60)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {key: "test-value" for key in runner.REQUIRED_ENV}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MOYAI_TEST_POSTGRES", raising=False)
    monkeypatch.setattr(runner, "cleanup_harbor_containers", Mock())
    return values


def test_output_lock_rejects_overlapping_run_and_releases_after_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="interrupted"), runner.run_lock(tmp_path):
        with pytest.raises(runner.RunAlreadyActive), runner.run_lock(tmp_path):
            pytest.fail("the second run acquired an active lock")
        raise ValueError("interrupted")
    with runner.run_lock(tmp_path):
        assert (tmp_path / ".run.lock").is_file()


def test_env_file_is_explicit_overrides_and_does_not_expand_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOYAI_EVAL_AGENT_MODEL", "inherited")
    monkeypatch.setenv("PRIVATE_TOKEN", "secret-shell-value")
    (tmp_path / ".env").write_text("MOYAI_EVAL_AGENT_MODEL=implicit\n")
    explicit = tmp_path / "chosen.env"
    explicit.write_text("MOYAI_EVAL_AGENT_MODEL=chosen\nLITERAL=${PRIVATE_TOKEN}\n")
    assert runner.load_environment(None)["MOYAI_EVAL_AGENT_MODEL"] == "inherited"
    loaded = runner.load_environment(explicit)
    assert loaded["MOYAI_EVAL_AGENT_MODEL"] == "chosen"
    assert loaded["LITERAL"] == "${PRIVATE_TOKEN}"
    assert runner.load_environment(None)["MOYAI_EVAL_AGENT_MODEL"] == "inherited"


def test_missing_configuration_generates_failure_report(
    arguments: argparse.Namespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in runner.REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    report = Mock(return_value={"all_passed": False})
    start = Mock()
    monkeypatch.setattr(runner, "write_report", report)
    monkeypatch.setattr(runner, "start_postgres", start)
    assert runner.execute(arguments) == 1
    assert "Missing evaluation configuration" in report.call_args.args[5]
    assert report.call_args.args[0].is_dir()
    assert "LITELLM_API_KEY" in report.call_args.args[5]
    start.assert_not_called()


def test_missing_explicit_env_file_still_generates_report(
    arguments: argparse.Namespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments.env_file = arguments.output_root / "missing.env"
    report = Mock(return_value={"all_passed": False})
    monkeypatch.setattr(runner, "write_report", report)
    assert runner.execute(arguments) == 1
    assert "Explicit environment file does not exist" in report.call_args.args[5]


def test_harbor_failure_cleans_only_owned_postgres_and_reports_failure(
    arguments: argparse.Namespace, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "preflight", Mock())
    start = Mock(return_value="postgresql://dedicated")
    stop = Mock()
    report = Mock(return_value={"all_passed": False})
    monkeypatch.setattr(runner, "start_postgres", start)
    monkeypatch.setattr(runner, "stop_postgres", stop)
    monkeypatch.setattr(runner, "run_harbor", Mock(side_effect=TimeoutError("run exceeded limit")))
    monkeypatch.setattr(runner, "write_report", report)
    assert runner.execute(arguments) == 1
    name = start.call_args.args[0]
    assert name.startswith("moyai-hourly-postgres-")
    assert stop.call_args.args[0] == name
    assert "run exceeded limit" in report.call_args.args[5]


def test_postgres_start_failure_still_attempts_cleanup(
    arguments: argparse.Namespace, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "preflight", Mock())
    start = Mock(side_effect=TimeoutError("database startup timed out"))
    stop = Mock()
    report = Mock(return_value={"all_passed": False})
    monkeypatch.setattr(runner, "start_postgres", start)
    monkeypatch.setattr(runner, "stop_postgres", stop)
    monkeypatch.setattr(runner, "write_report", report)
    assert runner.execute(arguments) == 1
    assert stop.call_args.args[0] == start.call_args.args[0]
    assert "database startup timed out" in report.call_args.args[5]


def test_external_postgres_is_never_managed(
    arguments: argparse.Namespace, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOYAI_TEST_POSTGRES", "postgresql://external")
    monkeypatch.setattr(runner, "preflight", Mock())
    start, stop, harbor = Mock(), Mock(), Mock()
    monkeypatch.setattr(runner, "start_postgres", start)
    monkeypatch.setattr(runner, "stop_postgres", stop)
    monkeypatch.setattr(runner, "run_harbor", harbor)
    monkeypatch.setattr(runner, "write_report", Mock(return_value={"all_passed": True}))
    assert runner.execute(arguments) == 0
    start.assert_not_called()
    stop.assert_not_called()
    assert harbor.call_args.args[3]["MOYAI_TEST_POSTGRES"] == "postgresql://external"


def test_successive_invocations_cannot_reuse_old_jobs(
    arguments: argparse.Namespace, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOYAI_TEST_POSTGRES", "postgresql://external")
    monkeypatch.setattr(runner, "preflight", Mock())
    harbor = Mock()
    monkeypatch.setattr(runner, "run_harbor", harbor)
    monkeypatch.setattr(runner, "write_report", Mock(return_value={"all_passed": True}))
    assert runner.execute(arguments) == 0
    assert runner.execute(arguments) == 0
    first, second = [call.args[1] for call in harbor.call_args_list]
    assert first != second
    assert first.parent.parent == second.parent.parent == arguments.output_root


def test_hung_process_gets_graceful_signal_then_group_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=123, poll=Mock(return_value=None))
    process.wait.side_effect = [subprocess.TimeoutExpired("harbor", 15), 0]
    kill = Mock()
    monkeypatch.setattr(runner.os, "killpg", kill)
    runner.stop_process_group(process)
    assert [call.args for call in kill.call_args_list] == [(123, signal.SIGINT), (123, signal.SIGKILL)]
    assert process.wait.call_count == 2


def test_run_timeout_stops_the_process_before_reporting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(wait=Mock(side_effect=subprocess.TimeoutExpired("harbor", 60)))
    launch, stop = Mock(return_value=process), Mock()
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner, "stop_process_group", stop)
    with pytest.raises(TimeoutError, match="60-second"):
        runner.run_harbor(tmp_path / "tasks", tmp_path / "jobs", tmp_path / "harbor.log", {}, 60)
    stop.assert_called_once_with(process)
    assert launch.call_args.kwargs["start_new_session"] is True


def test_docker_cleanup_requires_this_runs_bind_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = tmp_path / "fresh-run/jobs"
    owned = {
        "Id": "owned-container",
        "Config": {"Labels": {"com.docker.compose.project": "owned-project"}},
        "Mounts": [{"Type": "bind", "Source": str(jobs / "trial/agent"), "Destination": "/logs/agent"}],
    }
    other = {
        "Id": "other-container",
        "Config": {"Labels": {"com.docker.compose.project": "other-project"}},
        "Mounts": [{"Type": "bind", "Source": str(tmp_path / "other-run/jobs/trial/agent"),
                    "Destination": "/logs/agent"}],
    }
    unrelated = {
        "Id": "unrelated-container",
        "Config": {"Labels": {"com.docker.compose.project": "unrelated-project"}},
        "Mounts": [{"Type": "bind", "Source": str(jobs), "Destination": "/unrelated"}],
    }
    sidecar = {
        "Id": "owned-sidecar",
        "Config": {"Labels": {"com.docker.compose.project": "owned-project"}},
        "Mounts": [],
    }
    execute = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="owned-container\nother-container\nunrelated-container\nowned-sidecar\n"),
        subprocess.CompletedProcess([], 0, stdout=json.dumps([owned, other, unrelated, sidecar])),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0, stdout="owned-network\n"),
        subprocess.CompletedProcess([], 0),
    ])
    monkeypatch.setattr(runner.subprocess, "run", execute)
    runner.cleanup_harbor_containers(jobs, {})
    commands = [call.args[0] for call in execute.call_args_list]
    assert commands[2] == ["docker", "rm", "--force", "owned-container"]
    assert commands[3] == ["docker", "rm", "--force", "owned-sidecar"]
    assert "label=com.docker.compose.project=owned-project" in commands[4]
    assert commands[5] == ["docker", "network", "rm", "owned-network"]
    assert not any("prune" in command for command in commands)


def test_docker_cleanup_error_is_reported_as_failure(
    arguments: argparse.Namespace, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOYAI_TEST_POSTGRES", "postgresql://external")
    monkeypatch.setattr(runner, "preflight", Mock())
    monkeypatch.setattr(runner, "run_harbor", Mock())
    monkeypatch.setattr(runner, "cleanup_harbor_containers", Mock(side_effect=OSError("Docker unavailable")))
    report = Mock(return_value={"all_passed": False})
    monkeypatch.setattr(runner, "write_report", report)
    assert runner.execute(arguments) == 1
    assert "Harbor Docker cleanup failed" in report.call_args.args[5]


def test_report_pointer_is_fresh_and_history_does_not_include_delivery_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "run/report"
    output_dir.mkdir(parents=True)
    (output_dir / "report.json").write_text('{"all_passed": false}\n')
    (output_dir / "report.pdf").write_bytes(b"%PDF")
    (output_dir / "summary.txt").write_text("0% pass\n")
    execute = Mock(return_value=subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(runner.subprocess, "run", execute)
    monkeypatch.setattr(runner, "current_commit", lambda: "abc")
    result = runner.write_report(tmp_path / "run/jobs", tmp_path / "tasks", output_dir, tmp_path,
                                 "new-run", "missing configuration", {})
    assert not result["all_passed"]
    pointer = json.loads((tmp_path / "latest-report.json").read_text())
    assert pointer["run_id"] == "new-run"
    assert pointer["report_pdf"] == str(output_dir / "report.pdf")
    command = execute.call_args.args[0]
    assert command[command.index("--history-dir") + 1] == str(tmp_path / "history")
    assert command[command.index("--required-passes") + 1] == "2"


def test_report_failure_never_updates_previous_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = tmp_path / "latest-report.json"
    pointer.write_text('{"run_id": "previous"}\n')
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1)))
    monkeypatch.setattr(runner, "current_commit", lambda: "abc")
    with pytest.raises(RuntimeError, match="Report generation failed"):
        runner.write_report(tmp_path / "jobs", tmp_path / "tasks", tmp_path / "missing-report", tmp_path,
                            "broken-run", None, {})
    assert json.loads(pointer.read_text())["run_id"] == "previous"


def test_failure_details_redact_inherited_credentials() -> None:
    environment = {"LITELLM_API_KEY": "private-key", "MOYAI_TEST_POSTGRES": "postgresql://password"}
    detail = runner.redact_error(RuntimeError("failed private-key at postgresql://password"), environment)
    assert "private-key" not in detail
    assert "postgresql://password" not in detail


def test_harbor_overlay_keeps_the_production_lock_unchanged(tmp_path: Path) -> None:
    command = runner.harbor_command(tmp_path / "tasks", tmp_path / "jobs")
    assert "--frozen" in command
    assert "harbor==0.24.0" in command
    assert "openai==3.29.0" in command
    assert "litellm==1.107.0.dev3" in command
    assert command[command.index("-p") + 1] == str(tmp_path / "tasks")
    assert command[command.index("-k") + 1] == "3"
