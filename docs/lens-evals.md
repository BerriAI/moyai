# Evaluate a deployed Moyai build with Lens

Run an existing Lens evaluation from an ordinary test:

```python
from lens import Lens

lens = Lens(base_url="https://litellm-lens.onrender.com", api_key="<Lens eval key>")
lens.evals.run("moyai-coding-regressions").assert_passed()
```

The repository's [test](../evals/test_moyai.py) reads credentials from the environment and checks deployment readiness first. One test runs the complete dataset saved in Lens; there is no Python task adapter or dataset fixture to maintain.

The SDK runs inside your GitHub runner and calls both Lens and Moyai over HTTP. Moyai's own workers and configured sandboxes execute the agent, including its model and tool calls. Traces go directly from Moyai to Lens; the SDK uploads the completed output and retrieves the verdict. The Action then calls GitHub's API to publish the result.

This integration requires the named-eval server changes in [Lens PR #30](https://github.com/BerriAI/lens/pull/30). The SDK and Action are pinned to preview source commit `45b11ae53b740e77c0f8cea804bb4aa9bbc8f760`; older release wheels do not support this API. The Render URL is a configurable target, not a claim that its deployment is ready or includes that change.

## 1. Prepare the two services

Use a dedicated Moyai deployment with password login, a working model gateway and cloud sandbox, and a connected execution worker. Deploy this branch's evaluation metadata and tracing support. Set these **on the Moyai server**, through its deployment configuration:

```dotenv
TRACE_ENVIRONMENT=lens-eval
MOYAI_BUILD_SHA=<full lowercase SHA of the deployed Moyai build>
LITELLM_TRACE_ENDPOINT=https://litellm-lens.onrender.com/v1/traces
LITELLM_TRACE_API_KEY=<Lens tracing key>
```

Change the Lens origin to your own instance when needed. `MOYAI_BUILD_SHA` must come from the deployment's build metadata. Setting `LENS_VERSION` on a test runner does not deploy Moyai or change its emitted trace attributes.

On Lens, configure a judge model, create your dataset, and save `moyai-coding-regressions` using the [Moyai agent input/output contract](https://github.com/BerriAI/lens/blob/45b11ae53b740e77c0f8cea804bb4aa9bbc8f760/docs/agent-io.md#moyais-asynchronous-task-api). Choose the actual dataset ID and pinned revision, `agent: "moyai"`, and `connection: "moyai"`. The example polls the completed session, reads its summary, and correlates its `session.id`; keep that trace mapping for tool or cost checks. The saved definition owns the scorers, gates, trial count, and timeout.

The trusted connection profile is already in `pyproject.toml`. It resolves the deployment URL and password locally; saved Lens definitions do not contain those secrets. The named runner handles login, CSRF, submission, polling, and output extraction.

## 2. Run locally

Install [Rust through rustup](https://rustup.rs/) if necessary. The preview SDK builds its native extension from pinned source:

```sh
rustup toolchain install 1.99.0 --profile minimal
RUSTUP_TOOLCHAIN=1.99.0 uv sync --frozen --group eval
```

Set these in your shell or secret manager. Use an eval-capable Lens API key, separate from Moyai's tracing key:

```sh
export LENS_BASE_URL=https://litellm-lens.onrender.com
export LENS_API_KEY='<Lens API key allowed to read datasets/evals and create runs>'
export MOYAI_EVAL_URL='<HTTPS origin of the dedicated Moyai deployment>'
export MOYAI_EVAL_PASSWORD='<workspace password for that deployment>'
export LENS_VERSION='<full lowercase SHA actually deployed there>'
```

Then run:

```sh
uv run --frozen --group eval pytest evals/test_moyai.py -v --tb=short
```

Set `MOYAI_EVAL_NAME` to select another saved eval. `LENS_BASE_URL` can point to any compatible Lens server. A failed gate fails the test; it is not a successful result merely because the HTTP request completed.

The readiness check verifies `/api/config` reports the expected build, `lens-eval` environment, Lens trace configuration, cloud readiness, and worker connection. It never submits tasks. These are configuration/readiness checks; Lens's matched traces and returned outputs provide the evidence for the actual evaluation. To check readiness alone:

```sh
uv run --frozen python -m evals.preflight
```

The `eval` dependency group is optional. Normal `uv sync --frozen` and tests under `tests/` do not install or run the Lens SDK.

## 3. Run after deployment in GitHub Actions

Set repository variable `LENS_BASE_URL` to your Lens origin, such as `https://litellm-lens.onrender.com`. Set repository secrets `LENS_API_KEY` and `MOYAI_EVAL_PASSWORD`. The workflow's optional `lens-url` input overrides the variable.

[The Lens evals workflow](../.github/workflows/lens-evals.yml) accepts an explicit Moyai URL and deployed build SHA. It can be run manually from Actions or called by your deployment workflow. It does not deploy Moyai. Pull requests run a separate credential-free verification job that installs the pinned SDK from GitHub, imports the named client, and runs the offline readiness tests. That job does not contact an agent deployment or report evaluation quality; live evals require the explicit deployment inputs.

Append this job to a **trusted** deployment workflow whose `deploy` job already waits for readiness and exposes `agent-url` and `agent-build-sha` outputs:

```yaml
  lens-evals:
    needs: deploy
    permissions:
      contents: read
      checks: write
      pull-requests: write
    uses: ./.github/workflows/lens-evals.yml
    with:
      agent-url: ${{ needs.deploy.outputs.agent-url }}
      agent-build-sha: ${{ needs.deploy.outputs.agent-build-sha }}
      eval-name: moyai-coding-regressions
    secrets:
      LENS_API_KEY: ${{ secrets.LENS_API_KEY }}
      MOYAI_EVAL_PASSWORD: ${{ secrets.MOYAI_EVAL_PASSWORD }}
```

Adapt those output names to your real deployment. Do not pass a candidate SHA while leaving the URL on an older deployment: preflight rejects a mismatch with the server's build metadata. The calling workflow must permit the three permissions above. Fork and Dependabot pull requests are skipped because they do not receive the required secrets.

Run the workflow on the `main` branch against a deployed main build first to establish the baseline. Then call it after a trusted candidate deployment. Lens compares compatible dataset, scorer, gate, and agent-I/O definitions, publishes its run link and `Lens/<eval name>` check, and fails the job when a gate fails. A pull-request run with no compatible baseline can pass absolute gates but receives a neutral comparison check.

For manual runs, select the branch that produced the deployment and supply its exact build SHA. The workflow uses that SHA as `LENS_VERSION`, while GitHub branch/PR metadata still comes from the calling workflow.

## Troubleshooting

A preflight failure stops before creating a Lens run. Check the deployment's `/api/config` evaluation metadata and runtime configuration; preview-only local Moyai cannot execute this suite. Readiness metadata never returns a tracing key or workspace password.

An unsupported-contract error means the Lens server needs the named-eval changes. Missing eval or dataset errors mean the saved definition is absent, the pinned revision is wrong, or the Lens key lacks access.

A trace lookup failure requires the exact accepted `session.id`, `agent.name = moyai`, `agent.version = LENS_VERSION`, and `deployment.environment = lens-eval` on exported spans. Check that Moyai exports to the same Lens instance used by the test. A configured endpoint and key alone do not prove that trace delivery succeeded.

A failed gate is an evaluation result: open the Lens run to inspect the case output, trace, judge decision, and baseline comparison. For timeout behavior and deliberate process-crash resumption, see the [agent I/O guide](https://github.com/BerriAI/lens/blob/45b11ae53b740e77c0f8cea804bb4aa9bbc8f760/docs/agent-io.md#mapping-and-scoring-rules).
