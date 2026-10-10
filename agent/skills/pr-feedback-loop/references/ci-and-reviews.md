# Inspect CI and reviews through Moyai's GitHub tools

Discover current schemas instead of guessing arguments. Gateway credentials stay
on the server; these instructions do not require an authenticated `gh` CLI inside
the sandbox.

| Evidence needed | Tool and relevant selector |
| --- | --- |
| Current PR state, base and head | `github_pull_request` with repository ID and PR number |
| Existing review/issue/inline feedback | `github_pull_request_comments`; inspect every returned feedback category and pagination/limits |
| Checks and commit statuses | `github_ci_checks` with the **full current `head_sha`**; follow `next_page` |
| Workflow runs for that commit | `github_workflow_runs` with `head_sha`; follow `next_page` |
| Failed job and step | `github_workflow_jobs` with the matching `run_id`; follow `next_page` |
| Actual failure excerpt | `github_job_logs` with `job_id`; use `start_line` and `max_lines` for bounded excerpts |

Keep the observed commit, check name, state and evidence together. Read logs before
changing code; distinguish a deterministic regression from a runner/network failure.
An unrelated failing test still needs evidence that it predates the change before
calling it pre-existing. Do not disable checks to make a PR appear green.

## States that are easy to confuse

- **Pending/in progress:** the result is not known yet. Continue independent work
  and use the runtime's bounded wait if available. Avoid tight polling loops.
- **Empty checks:** no checks were observed. Inspect workflow runs and repository
  requirements. Never infer success from an empty array.
- **Incomplete page, truncated feedback or unavailable logs:** report the evidence
  gap. Follow supported pagination/read tools; do not call a partial response complete.
- **Skipped, cancelled or neutral:** identify whether the required behavior was
  actually exercised. Do not count these as successful verification by default.
- **A newer head appears:** inspect the delta, preserve others' work, and repeat
  checks on the new head. Do not replay an old publication over the new changes.
- **Old approvals or bot comments:** use their commit and subsequent discussion to
  decide whether they still apply. A resolved thread alone does not prove a fix.
- **403/permission failure:** use the tool's connection/access recovery guidance.
  Do not relabel it as a flaky test or fall back to a different account.

Moyai's CI tools are read-only. Do not invent a workflow-rerun or review-resolution
tool. If an explicitly requested rerun, review request or thread operation is absent
from the current catalog, report that limit and complete the repair work you can do.

When asked to communicate findings, use only supported authorized comment tools.
Prefer one concise response identifying the finding, relevant commit and evidence.
Never claim a review was requested, a thread was resolved or CI passed without the
corresponding confirmed result.
