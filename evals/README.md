# Moyai agent evaluations

The evaluation runner runs every task under `evals/tasks/` in Harbor 0.24.0, with three attempts per task. A task passes when at least two attempts pass its verifier. The headline percentage is the fraction of tasks meeting that gate; an infrastructure failure prevents an all-pass result even if some trials succeeded. A fresh report is generated for failed runs and configuration errors too.

## Run and report

Start Docker and set `LITELLM_API_BASE`, `LITELLM_API_KEY`, `MOYAI_EVAL_AGENT_MODEL`, and `MOYAI_EVAL_JUDGE_MODEL`. Run from the repository root:

```sh
PYTHONPATH=evals uv run --no-project --python 3.13 \
  --with reportlab==4.4.10 --with 'python-dotenv>=1,<2' \
  python -m moyai_harbor.run_report --output-root evals/reports
```

To use a specific environment file, add `--env-file /absolute/path/to/eval.env`. Only an explicitly supplied file is loaded, and its values override the inherited environment. Values are read literally without expanding references to other environment variables. Environment files and credentials must remain outside version control.

If `MOYAI_TEST_POSTGRES` is unset, the runner starts a disposable `postgres:18-alpine` container on a randomly assigned loopback port and removes that container after the run. If it is set, the runner uses the supplied evaluation database without managing its container. Use a dedicated evaluation database. Each run has a unique ID and a separate jobs directory; a lock prevents overlapping invocations against the same output root. `--timeout-seconds` defaults to 2700 (45 minutes), followed by a short graceful shutdown and forced termination if needed. `--tasks-root` can select a different Harbor task directory.

Every completed invocation saves:

- `<output-root>/<run-id>/report/summary.txt`: actual pass percentage and each task's pass/fail result.
- `<output-root>/<run-id>/report/report.pdf`: per-task score, duration, and recorded inference cost graphs, plus historical comparisons when previous reports exist.
- `<output-root>/<run-id>/report/report.json`: structured results and metrics.
- `<output-root>/<run-id>/jobs/` and `harbor.log`: private raw run artifacts for debugging.
- `<output-root>/latest-report.json`: an atomic pointer to the latest completed report, with its run ID and artifact paths.
- `<output-root>/history/`: bounded report history used for trend graphs.

Unknown or partial costs are labeled as such; missing receipts are not counted as free. Raw jobs can contain credentials and private model context, so only the generated reports should be shared. Exit status is `0` for a passing report, `1` for a failed run or report, and `2` when another invocation already owns the output root.

The hourly Codex automation invokes this runner and uses the Slack connector to send `summary.txt` with `report.pdf` to `#product-team`. The automation should publish the fresh run even when the runner exits `1`, and skip publication on exit `2` to avoid reporting another run's results. Scheduling and Slack credentials are managed outside this repository. Keep the output root persistent so reports retain history, and track the last posted run ID to prevent duplicate messages. The GitHub workflow supports pull requests, pushes to `main`, and manual dispatch; it deliberately has no hourly schedule. Pull request and push runs skip live evaluation if configuration is missing. Manual dispatch instead produces a failure report for missing configuration.

## Dependencies and verification

Harbor is installed as a temporary `uv` overlay so it does not change the native production dependency lock. The overlay pins `harbor==0.24.0`, `openai==3.29.0`, and `litellm==1.107.0.dev3`. The pinned LiteLLM prerelease allows OpenAI 3.x; the stable LiteLLM constraint `openai<3` is incompatible with the native application's OpenAI pin. The standalone reporting command requires ReportLab and python-dotenv, not the production application dependencies.

Run the reporting tests without live credentials (Node.js 22 is needed for the native package build):

```sh
PYTHONPATH=.:evals:native/src uv run --project native --frozen --python 3.13 \
  --with reportlab==4.4.10 --with pypdf \
  pytest -q evals/tests
```

Regenerate a report from saved artifacts:

```sh
PYTHONPATH=evals uv run --no-project --python 3.13 --with reportlab==4.4.10 \
  python -m moyai_harbor.report \
  --jobs-root /absolute/path/to/run/jobs --tasks-root evals/tasks \
  --output-dir /absolute/path/to/run/report --history-dir /absolute/path/to/reports/history \
  --attempts 3 --required-passes 2 --run-id saved-run
```

## Add a task

Create a directory under `evals/tasks/` containing `task.toml`, `instruction.md`, an `environment/` with its Dockerfile and fixtures, and `tests/` with a `test.sh` verifier. Keep task-specific fixtures in that task's environment. New tasks are automatically included in the next run. The `moyai_harbor` package provides the Moyai agent and turn runner shared by tasks.

`evals/moyai_harbor/workspace.py` contains an eval-only `WorkspaceRef(provider="modal")` shim. It presents Harbor's isolated environment as the Modal workspace reference expected by credential execution, while forwarding commands to Harbor's environment. It is not a production workspace backend.
