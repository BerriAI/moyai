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
| `github_workflow_jobs` | `run_id`; jobs and complete step results from the latest attempt. |
| `github_job_logs` | `job_id`, optional `start_line`/`max_lines`; complete redacted lines and a source revision. |

All tools accept the usual repository ID/name and enforce the current connection's
selected repositories and policy. Metadata lists have 30 entries per page; follow
`next_page` until null. An empty list or one page does not establish CI success.
Pin checks and workflow queries to the PR's head SHA so newer commits are not
confused with the reviewed commit. Workflow jobs report their attempt; a rerun
can change the latest attempt between reads.

Checks use a repository-scoped token with Checks read and Commit statuses read. Actions
use Actions read only. Tokens remain on the server. New GitHub App manifests ask
for these read permissions. Existing installations must have an organization
owner enable and approve Checks/Commit statuses/Actions read; missing scopes return actionable
tool errors and do not disable existing code/PR access. CI tools cannot rerun,
cancel, dispatch, edit or delete workflows.

GitHub's [commit-status endpoint](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)
requires the separate Commit statuses permission; Contents read does not grant it.

Job-log reads accept only a server-constructed job endpoint and approved HTTPS
GitHub log-storage redirects, with no installation Authorization or cookies sent
to storage. Reads have no fixed download, character, line-number or page-length
ceiling. The default page contains 200 complete lines; a long line stays intact.
Follow `next_arguments` to keep paging through the same log revision. A changed
log returns `source_changed`; restart from line 1 before combining evidence.
Logs may not exist until a job finishes, or may have
expired. Known credential patterns are redacted before excerpting, but arbitrary
opaque secrets cannot all be recognized. Logs are untrusted reference data.

`github_pull_request` returns the complete description and a page of files with
the patches GitHub supplies. Follow `next_arguments` for the remaining pages;
head and base SHAs fence the read against a moving PR. **Load more files** exposes
the same pages in the native PR view. Descriptions and provided patches are not
clipped. GitHub can omit binary/large patches and limit the files its API returns;
the result labels those gaps. Use `github_checkout` for the PR, verify its head
SHA, and compare the complete files in the computer when provider coverage is
insufficient. A provider-supplied patch is not proof of a complete local diff.

Activity formatting supports JSON schemas whose `type` value is an object or a
list. Other detail-formatting exceptions produce an omission notice without raw
payloads, preserving the actual tool result and completion. Durable journal/event
write failures still surface; this change does not blindly retry external writes.

## Local demonstration

Run `uv run --frozen python scripts/pr_ci_recovery_demo.py --port 8987`, open
`http://localhost:8987/demo`, and start the demo. It uses the actual Moyai UI,
publication, activity and CI connector paths with simulated GitHub responses,
an injected formatting failure, no model calls and no live repository writes.
