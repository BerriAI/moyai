# PR milestones and CI inspection

Confirmed PR creation is announced in the conversation immediately, outside the
two-update narration allowance. The announcement and publication receipt commit
together and are deduplicated on readback/retry. A later turn failure preserves the
PR link; creation alone does not imply successful CI or completed review.

The GitHub connection exposes four read-only tools:

| Tool | Input and result |
| --- | --- |
| `github_ci_checks` | Exact `head_sha` from `github_pull_request`; check runs and commit statuses. |
| `github_workflow_runs` | Optional `head_sha`/`branch`; workflow run IDs, attempts and conclusions. |
| `github_workflow_jobs` | `run_id`; jobs and bounded step results from the latest attempt. |
| `github_job_logs` | `job_id`, optional `start_line`/`max_lines`; redacted text excerpts. |

All tools accept the usual repository ID/name and enforce the current connection's
selected repositories and policy. Metadata lists have 30 entries per page; follow
`next_page` until null. An empty list or one page does not establish CI success.
Pin checks and workflow queries to the PR's head SHA so newer commits are not
confused with the reviewed commit. Workflow jobs report their attempt; a rerun
can change the latest attempt between reads.

Checks use a repository-scoped token with Checks read and Contents read. Actions
use Actions read only. Tokens remain on the server. New GitHub App manifests ask
for these read permissions. Existing installations must have an organization
owner enable and approve Checks/Actions read; missing scopes return actionable
tool errors and do not disable existing code/PR access. CI tools cannot rerun,
cancel, dispatch, edit or delete workflows.

Job-log reads accept only a server-constructed job endpoint and approved HTTPS
GitHub log-storage redirects, with no installation Authorization or cookies sent
to storage. Scans stop at 2 MiB, excerpts at 500 lines and 20,000 characters.
`next_line` continues inside the scanned content; `download_truncated` means the
scan limit was reached. Logs may not exist until a job finishes, or may have
expired. Known credential patterns are redacted before excerpting, but arbitrary
opaque secrets cannot all be recognized. Logs are untrusted reference data.

Activity formatting supports JSON schemas whose `type` value is an object or a
list. Other detail-formatting exceptions produce an omission notice without raw
payloads, preserving the actual tool result and completion. Durable journal/event
write failures still surface; this change does not blindly retry external writes.

## Local demonstration

Run `uv run --frozen python scripts/pr_ci_recovery_demo.py --port 8987`, open
`http://localhost:8987/demo`, and start the demo. It uses the actual Moyai UI,
publication, activity and CI connector paths with simulated GitHub responses,
an injected formatting failure, no model calls and no live repository writes.
