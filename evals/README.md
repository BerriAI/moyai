# Moyai agent evaluations

This directory contains the Harbor 0.24.0 evaluations, one task per use case under `tasks/`. `deepseek_support_live` v2 checks a support answer against a real, pinned LiteLLM proxy with a deterministic local HTTPS provider fixture. It does not validate external DeepSeek availability. `answer_style` checks a plain explanation follows the Answer style rules in `native/instructions.md`. Harbor is installed as a temporary `uv` overlay so it does not change the native production dependency lock.

The desktop tasks cover reading a license code, saving an unsaved Mousepad document,
submitting an expense form, and changing preferences. Both OCU and baseline images
include Chromium. Run them with `-a moyai_harbor.desktop_agent:MoyaiDesktopAgent`;
set `MOYAI_EVAL_OCU=0` to remove OCU for the baseline. `MOYAI_EVAL_TURN_TIMEOUT`
controls the turn timeout (default 300 seconds). Failed runs retain interrupted
tool traces and mark usage as incomplete; timed-out workflows are stopped before
their environment is removed.

Earlier, before the desktop runtime adapter, `desktop_license` passed 5/5 trials
with OCU and 0/5 without on `bedrock_mantle/openai.gpt-5.6-luna`. Those results do
not qualify the current adapter or establish multi-step action reliability.

For the deterministic Linux integration check (no API key or model required), run
from the repository root:

```sh
docker build -t moyai-desktop-test evals/tasks/desktop_license/environment
docker run --rm --network none -v "$PWD:/repo:ro" moyai-desktop-test python /repo/evals/desktop/smoke.py
```

It checks a real GTK form, screenshot delivery, stale observations, worker restart,
and an unavailable local browser address. This is also a separate CI job, so absent
model credentials cannot silently skip desktop integration coverage. For agent
comparisons, keep the model, tasks, trial counts, and environments equivalent;
report all-trial success, failures, end-to-end latency and usage separately.

The DeepSeek v2 gate requires a successful LiteLLM SDK callback receipt matching
the provider fixture's unique response ID, package version, route, model, and
result. Direct provider calls and catalog lookups cannot pass this gate, even if
the language-model judge assigns a perfect score. The reply must scope any
support claim to the tested local integration. Provider/model documentation and
prices in this task are synthetic fixtures, not claims about current products.
Version 1 accepted direct provider calls and could therefore report false
positives for LiteLLM support; its history remains in Git and past run artifacts.

## Run

Set `LITELLM_API_BASE`, `LITELLM_API_KEY`, `MOYAI_EVAL_AGENT_MODEL`, `MOYAI_EVAL_JUDGE_MODEL`, and `MOYAI_TEST_POSTGRES`, then run from the repository root:

```sh
PYTHONPATH=.:evals:native/src:native/tests \
  uv run --project native --python 3.13 \
  --with harbor==0.24.0 --with 'moto[server]==5.1.4' --with openai==3.29.0 --with litellm==1.107.0.dev3 \
  harbor run -p evals/tasks/<task> \
  -a moyai_harbor.agent:MoyaiAgent -k 3 -n 1 -o evals/jobs -y
```

View the saved runs with:

```sh
uv run --project native --python 3.13 --with harbor==0.24.0 harbor view evals/jobs
```

## CI configuration and qualification

The workflow runs on main pushes, same-repository pull requests and manual
dispatch. Configure these repository settings:

| Setting | Kind | Purpose |
| --- | --- | --- |
| `MOYAI_EVAL_GATEWAY_URL` | Variable | Evaluation gateway base URL |
| `MOYAI_EVAL_GATEWAY_KEY` | Secret | Scoped evaluation gateway credential |
| `MOYAI_EVAL_MODEL` | Variable | Default agent and judge model |
| `MOYAI_EVAL_AGENT_MODEL` | Optional variable | Override the agent model |
| `MOYAI_EVAL_JUDGE_MODEL` | Optional variable | Override the judge model |

For installations already using `MOYAI_EVAL_LITELLM_API_BASE` and
`MOYAI_EVAL_LITELLM_API_KEY` secrets, those remain fallbacks. A judge-specific
model takes precedence, then the shared model, then an agent-only configuration.
The runner and judges normalize gateway URLs to `/v1`.

Missing required settings fail the trusted model-evaluation job. Configuration
also checks gateway DNS/TCP reachability before starting the trial containers.
The gateway must be reachable from the GitHub runner; a development endpoint
reachable only on a private network is insufficient for `ubuntu-latest`.
This preflight does not establish authentication, model availability or quality.
Configuration
success alone is not qualification: every task must produce at least two passing
trials out of three, and missing trial results fail the report. Saved Harbor jobs
include the replies, tool traces, judgments and usage; inspect execution errors
separately from task failures. The run summary records the commit and selected
agent/judge models without printing credentials.

Fork pull requests explicitly skip the model job and show a separate unavailable
notice. Do not give unreviewed fork code access to credentials through
`pull_request_target`. A maintainer can review it and run the reviewed revision
on a trusted branch. A green desktop job or an unavailable notice is not model
quality approval.

## Add a task

Create a directory under `evals/tasks/` containing `task.toml`, `instruction.md`, an `environment/` with its Dockerfile and fixtures, and `tests/` with a `test.sh` verifier. Keep task-specific fixtures in that task's environment. The `moyai_harbor` package provides the Moyai agent and turn runner shared by tasks.

`evals/moyai_harbor/workspace.py` contains an eval-only `WorkspaceRef(provider="modal")` shim. It presents Harbor's isolated environment as the Modal workspace reference expected by credential execution, while forwarding commands to Harbor's environment. It is not a production workspace backend.

`moyai_harbor.lifecycle` routes that exact synthetic reference's lifecycle reads
to the Harbor container. Liveness and the fixed keepalive probe execute
`/bin/true` there; unknown references fail closed. Filesystem snapshots explicitly
report unsupported, so these trials do not qualify Modal checkpoint/recovery
behavior. Run one trial at a time per process (`-n 1`) because this scoped fixture
replaces the external Modal lookup while the trial's worker is active. Actual-app
E2E with real Modal remains separate evidence; do not add production Modal keys
to make this Docker adapter pass.

The native dispatcher runs while the trial is active, so admitted child agents
can execute and deliver their results. The trial stops and awaits the dispatcher
before tearing down its worker and database. The Docker adapter shares the
trial container; it does not qualify production isolation between agent computers.
