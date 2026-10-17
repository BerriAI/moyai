"""Run one isolated Harbor evaluation and persist its report for the hourly publisher."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
REQUIRED_ENV = (
    "LITELLM_API_BASE", "LITELLM_API_KEY", "MOYAI_EVAL_AGENT_MODEL", "MOYAI_EVAL_JUDGE_MODEL",
)
EVAL_OVERLAYS = ("harbor==0.24.0", "openai==3.29.0", "litellm==1.107.0.dev3")


class RunAlreadyActive(RuntimeError):
    """Another invocation owns this output directory."""


@contextmanager
def run_lock(output_root: Path) -> Iterator[None]:
    """Keep the file in place: unlinking a flock file can let two owners in."""
    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / ".run.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunAlreadyActive("An evaluation is already running for this output directory.") from error
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(f"{os.getpid()}\n")
            stream.flush()
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def load_environment(env_file: Path | None) -> dict[str, str]:
    environment = dict(os.environ)
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError(f"Explicit environment file does not exist: {env_file}")
        from dotenv import dotenv_values

        values = dotenv_values(env_file, interpolate=False)
        environment.update({key: value for key, value in values.items() if value is not None})
    return environment


def preflight(environment: dict[str, str], tasks_root: Path) -> None:
    missing = [key for key in REQUIRED_ENV if not environment.get(key, "").strip()]
    if missing:
        raise ValueError(f"Missing evaluation configuration: {', '.join(missing)}")
    if not tasks_root.is_dir() or not any(tasks_root.rglob("task.toml")):
        raise ValueError(f"No Harbor tasks found at {tasks_root}")
    for program in ("uv", "docker"):
        if shutil.which(program, path=environment.get("PATH")) is None:
            raise ValueError(f"Required program is unavailable: {program}")
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("Docker is unavailable; start Docker and retry the evaluation.")


def start_postgres(container_name: str, environment: dict[str, str]) -> str:
    subprocess.run(
        ["docker", "run", "--detach", "--rm", "--name", container_name,
         "--label", "moyai.hourly-eval=true", "--publish", "127.0.0.1::5432",
         "--env", "POSTGRES_PASSWORD=eval-only", "--env", "POSTGRES_DB=postgres", "postgres:18-alpine"],
        env=environment, capture_output=True, text=True, timeout=120, check=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
            env=environment, capture_output=True, text=True, timeout=10, check=False,
        )
        if ready.returncode == 0:
            port = subprocess.run(
                ["docker", "port", container_name, "5432/tcp"],
                env=environment, capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip().splitlines()[0].rsplit(":", 1)[-1]
            if not port.isdigit():
                raise RuntimeError("Docker did not return a valid PostgreSQL port.")
            return f"postgresql://postgres:eval-only@127.0.0.1:{port}/postgres"
        time.sleep(1)
    raise TimeoutError("The disposable PostgreSQL database did not become ready within 60 seconds.")


def stop_postgres(container_name: str, environment: dict[str, str]) -> None:
    subprocess.run(
        ["docker", "rm", "--force", container_name],
        env=environment, capture_output=True, text=True, timeout=30, check=True,
    )


def cleanup_harbor_containers(jobs_root: Path, environment: dict[str, str]) -> None:
    """Clean surviving daemon resources only when bind mounts prove this run owns them."""
    listed = subprocess.run(
        ["docker", "ps", "--all", "--filter", "label=com.docker.compose.project", "--format", "{{.ID}}"],
        env=environment, capture_output=True, text=True, timeout=15, check=True,
    )
    container_ids = listed.stdout.split()
    if not container_ids:
        return
    inspected = subprocess.run(
        ["docker", "inspect", *container_ids], env=environment, capture_output=True,
        text=True, timeout=30, check=True,
    )
    containers = json.loads(inspected.stdout)
    projects = set()
    for container in containers:
        mounts = container.get("Mounts", [])
        owned = any(
            mount.get("Type") == "bind"
            and mount.get("Destination") in ("/logs/agent", "/logs/verifier")
            and Path(mount.get("Source", "")).resolve().is_relative_to(jobs_root.resolve())
            for mount in mounts
        )
        project = container.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
        if owned and project:
            projects.add(project)
    for container in containers:
        project = container.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
        if project not in projects:
            continue
        subprocess.run(
            ["docker", "rm", "--force", container["Id"]], env=environment, capture_output=True,
            text=True, timeout=30, check=True,
        )
    for project in projects:
        networks = subprocess.run(
            ["docker", "network", "ls", "--filter", f"label=com.docker.compose.project={project}",
             "--format", "{{.ID}}"], env=environment, capture_output=True, text=True, timeout=15, check=True,
        ).stdout.split()
        if networks:
            subprocess.run(
                ["docker", "network", "rm", *networks], env=environment, capture_output=True,
                text=True, timeout=30, check=True,
            )


def harbor_command(tasks_root: Path, jobs_root: Path) -> list[str]:
    command = ["uv", "run", "--frozen", "--project", str(REPOSITORY / "native"), "--python", "3.13"]
    for requirement in EVAL_OVERLAYS:
        command.extend(["--with", requirement])
    return [*command, "harbor", "run", "-p", str(tasks_root),
            "-a", "moyai_harbor.agent:MoyaiAgent", "-k", "3", "-n", "1", "-o", str(jobs_root), "-y"]


def stop_process_group(process: subprocess.Popen, *, grace_seconds: int = 15) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def run_harbor(tasks_root: Path, jobs_root: Path, log_path: Path,
               environment: dict[str, str], timeout_seconds: int) -> None:
    with log_path.open("w") as log:
        process = subprocess.Popen(
            harbor_command(tasks_root, jobs_root), cwd=REPOSITORY, env=environment,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            stop_process_group(process)
            raise TimeoutError(f"Harbor exceeded the {timeout_seconds}-second run limit.") from error
        except BaseException:
            stop_process_group(process)
            raise
    if returncode:
        raise RuntimeError(f"Harbor exited with status {returncode}; see harbor.log for details.")


def redact_error(error: BaseException, environment: dict[str, str]) -> str:
    message = f"{type(error).__name__}: {error}"
    for key, value in environment.items():
        if value and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "POSTGRES")):
            message = message.replace(value, "[redacted]")
    return message[:2000]


def current_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, capture_output=True, text=True,
            timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_report(jobs_root: Path, tasks_root: Path, output_dir: Path, output_root: Path,
                 run_id: str, run_error: str | None, environment: dict[str, str]) -> dict:
    command = [sys.executable, "-m", "moyai_harbor.report", "--jobs-root", str(jobs_root),
               "--tasks-root", str(tasks_root), "--output-dir", str(output_dir),
               "--history-dir", str(output_root / "history"), "--attempts", "3", "--required-passes", "2",
               "--run-id", run_id, "--commit", current_commit()]
    if environment.get("GITHUB_RUN_ID") and environment.get("GITHUB_REPOSITORY"):
        run_url = (f"{environment.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{environment['GITHUB_REPOSITORY']}/actions/runs/{environment['GITHUB_RUN_ID']}")
        command.extend(["--run-url", run_url])
    if run_error:
        command.extend(["--run-error", run_error])
    # A failed evaluation is valid report output; inspect the file rather than the CLI exit status.
    completed = subprocess.run(command, cwd=REPOSITORY, env=environment, timeout=120, check=False)
    report_path = output_dir / "report.json"
    if not report_path.is_file():
        raise RuntimeError(f"Report generation failed with status {completed.returncode}.")
    report = json.loads(report_path.read_text())
    if not (output_dir / "report.pdf").is_file() or not (output_dir / "summary.txt").is_file():
        raise RuntimeError("Report generation did not produce all expected artifacts.")
    pointer = {
        "run_id": run_id,
        "report_dir": str(output_dir),
        "report_json": str(report_path),
        "report_pdf": str(output_dir / "report.pdf"),
        "summary_txt": str(output_dir / "summary.txt"),
        "harbor_log": str(jobs_root.parent / "harbor.log"),
        "all_passed": report.get("all_passed") is True,
    }
    temporary = output_root / f".latest-report-{run_id}.json"
    temporary.write_text(json.dumps(pointer, indent=2) + "\n")
    temporary.replace(output_root / "latest-report.json")
    return report


def execute(args: argparse.Namespace) -> int:
    output_root = args.output_root.expanduser().resolve()
    tasks_root = args.tasks_root.expanduser().resolve()
    with run_lock(output_root):
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}"
        run_root = output_root / run_id
        jobs_root = run_root / "jobs"
        report_dir = run_root / "report"
        jobs_root.mkdir(parents=True)
        report_dir.mkdir()
        environment = dict(os.environ)
        errors = []
        container_name = None
        harbor_started = False
        try:
            environment = load_environment(args.env_file)
            environment["PYTHONPATH"] = os.pathsep.join([
                str(REPOSITORY), str(REPOSITORY / "evals"), str(REPOSITORY / "native/src"),
                str(REPOSITORY / "native/tests"), environment.get("PYTHONPATH", ""),
            ])
            preflight(environment, tasks_root)
            if not environment.get("MOYAI_TEST_POSTGRES", "").strip():
                container_name = f"moyai-hourly-postgres-{run_id.lower()}"
                environment["MOYAI_TEST_POSTGRES"] = start_postgres(container_name, environment)
            harbor_started = True
            run_harbor(tasks_root, jobs_root, run_root / "harbor.log", environment, args.timeout_seconds)
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - persist failures for the hourly report
            errors.append(redact_error(error, environment))
        finally:
            if harbor_started:
                try:
                    cleanup_harbor_containers(jobs_root, environment)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    errors.append(f"Harbor Docker cleanup failed: {redact_error(error, environment)}")
            if container_name:
                try:
                    stop_postgres(container_name, environment)
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(f"Disposable PostgreSQL cleanup failed: {redact_error(error, environment)}")
        # Environment-file failures must still be reportable from a fresh Python process.
        environment["PYTHONPATH"] = os.pathsep.join([
            str(REPOSITORY / "evals"), environment.get("PYTHONPATH", ""),
        ])
        report = write_report(jobs_root, tasks_root, report_dir, output_root, run_id,
                              "; ".join(errors) or None, environment)
        print(f"Harbor report: {report_dir}")
        return 0 if report.get("all_passed") is True and not errors else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Explicit dotenv file; values override the inherited environment.")
    parser.add_argument("--output-root", type=Path, default=REPOSITORY / "evals/reports")
    parser.add_argument("--tasks-root", type=Path, default=REPOSITORY / "evals/tasks")
    parser.add_argument("--timeout-seconds", type=int, default=2700)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    def interrupted(signum: int, _frame: object) -> None:
        raise InterruptedError(f"Evaluation interrupted by signal {signum}.")

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        return execute(args)
    except RunAlreadyActive as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - report persistence errors need a concise CLI failure
        print(f"Could not persist the Harbor report: {redact_error(error, dict(os.environ))}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
