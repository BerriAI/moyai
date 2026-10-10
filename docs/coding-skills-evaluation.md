# Coding skill pack: initial evaluation

On 2026-10-10, all four controlled workflow cases met the three predefined rubric
items both with and without the pack. The model automatically loaded the relevant
skill in every treatment case. This validates selection and usable guidance; it
does **not** demonstrate a general improvement in coding quality.

## Method

`scripts/coding_skills_eval.py` runs the current production core prompt with
`openai/gpt-6-astra`, `reasoning_effort=high`, and the same read-only evidence/tools
in both conditions. Each case gets a fresh, disposable library. The baseline has
no skills; the treatment installs the three bundled skills. Actual `Skills`
discovery, loading, private context and reference reads are used, with no forced
skill selection. The script's standalone Chat Completions loop is not a production
sandbox or harness benchmark.

There is one sample per case/condition, with condition order alternated. The three
rubric items per case are fixed in the script before inference, withheld from the
model, and assessed manually from the saved responses and tool traces.

| Case | Observed result in both conditions | Without: calls / seconds | With: calls / seconds |
| --- | --- | --- | --- |
| Moyai storage verification | Focused locked-dependency checks, SQLite and disposable PostgreSQL, no false execution claim | 5 / 30.6 | 4 / 26.6 |
| Two review findings | Confirmed omitted blank filtering; dismissed the claim that dictionaries sort keys; proposed regression cases | 2 / 14.6 | 3 / 17.2 |
| New PR head with empty checks | Reported queued current-head CI; did not reuse old passing CI or old review | 3 / 10.1 | 3 / 12.4 |
| Blocked PR delivery | Reported local fix/tests and demo, read-only publication blocker, absent captures and unrun CI | 2 / 10.3 | 3 / 13.8 |

The treatment loaded `moyai-dev-and-test` for the repository-specific case,
`pr-feedback-loop` for the two PR inspection cases, and `pr-delivery` for handoff.
References loaded only when requested by the model. The Moyai-specific runbook
was not loaded for the other repositories.

## Overhead and limits

Across these four tasks, the baseline used 12 model calls and 65.6 seconds; the
pack used 13 calls and 70.0 seconds. Reported input tokens summed across calls
were 69,389 versus 75,850; output tokens were 2,909 versus 3,028. Gateway-reported
cost was approximately $0.488 versus $0.960. Cached input tokens were 45,589 versus
12,140, so these costs should not be projected onto production workloads.
Loading instructions changes private context and can reduce cache reuse.

The initial three-description directory adds 694 serialized characters over an
empty directory (696 characters including the existing list brackets). It includes
no instruction bodies or references. Application startup installs local release
files; ordinary inference does not read those files or start a sandbox to discover
them. This is a loading contract, not a measured improvement in container startup.

The cases use recorded repository/PR evidence and involve no code execution,
patching, live GitHub writes or real CI. A capable baseline already handled all
four cases correctly. Repeated end-to-end repair/delivery tasks and production
cache/latency measurements are needed before claiming the pack makes Moyai smarter.

## Reproduce

Configure an endpoint-specific API key in the environment without writing it into
the checkout. The evaluator defaults to `LITELLM_API_KEY`; `--api-key-env` selects
another configured variable when development and production use different keys.

```sh
uv run --frozen python scripts/coding_skills_eval.py \
  --base-url "$LITELLM_API_BASE" --model openai/gpt-6-astra \
  --reasoning high --api-key-env LITELLM_DEV_API_KEY \
  --output /tmp/moyai-coding-skills-evaluation.json
```

This makes billed requests to the configured gateway. The JSON retains the inputs,
rubric, final answers, tool results, usage, latency and reported cost. It never
records the API key or request headers. The local loading demo is independent:

```sh
uv run --frozen python scripts/coding_skills_demo.py --port 8843
```

Open `http://127.0.0.1:8843/demo/login` for the real Skills library, then
`http://127.0.0.1:8843/demo/probe` for discovery, instruction and reference loading.
This demo uses disposable SQLite data with no model requests or sandbox processes.
