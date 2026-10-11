# GitHub coding-tool audit

Audited against main `2d106bfba6cd1bd4af568298cd37a30ec07c770a` and the connected GitHub
connector catalog on October 11, 2026. Scope: the full coding workflow. Merging,
auto-merge, approval, review dismissal, repository administration and generic
credential-bearing API access are not added.

## The reported publication failure

The Slack run for PR #400 already had the publishing tool and exact-PR access.
Both `agent/tools/github_tools.py` and `app/github.py` independently rejected every
`.env`-prefixed path. Approving PR access could not change that path policy.
`.env.example` only changed `AGENT_HARNESS=pydantic-ai` to
`AGENT_HARNESS=deepagents-temporal`. GitHub #400 was subsequently updated; the old
Slack message does not describe its current head.

A shared path policy now permits exactly `.env.example`, `.env.sample`, and
`.env.template` as file basenames, including nested files. It still rejects real
environment files, template lookalikes with extra suffixes, `.env*` directories,
SSH material, private-key extensions, CODEOWNERS and unsafe paths. Template names
do not establish that their contents are secret-free; review template values as
part of the ordinary change review. Existing tree/symlink checks still apply.

## Coverage

| Capability | Before | After |
| --- | --- | --- |
| Selected repository discovery, checkout and source context | Available | Existing tools retained |
| File creation/update/deletion, blobs/trees/commits/branches | Available through PR publication | Existing atomic publication pipeline retained; environment templates fixed |
| Workflow and binary file publication | Available | Existing token scopes and binary transport retained |
| PR descriptions/diffs/files/comments/review summaries | Available | Existing paginated readers retained |
| Checks, statuses, workflow runs/jobs/logs | Available | Existing exact-head readers retained |
| Rulesets and reviewer configuration | Available | Existing tools unchanged |
| Code/issue/PR/commit search | Missing (source context searched only a cached path/document index) | `github_search`, scoped to one selected repository |
| Issue reading/listing/comments | Missing | `github_issues`, `github_issue`, `github_issue_comments` |
| PR listing, branches, commits/comparisons, files/directories | Missing dedicated tools | `github_pull_requests`, `github_branches`, `github_commits`, `github_commit`, `github_compare_commits`, `github_file` |
| Label/milestone/release discovery | Missing | `github_labels`, `github_milestones`, `github_releases` |
| Issues: create/edit/close/reopen/comment/edit comment | Missing | `github_create_issue`, `github_update_issue`, `github_comment_issue`, `github_edit_issue_comment` |
| PR title/body/labels/assignees | Missing; `github_update_pull_request` only publishes files | `github_update_pull_request_details` |
| Request/remove reviewers | Missing | `github_pull_request_reviewers` |
| Inline comments and comment-only reviews | Missing | `github_comment_review` always submits `COMMENT`; the schema has no event argument |
| Reply/edit review comments | Missing | `github_reply_review_comment`, `github_edit_pull_request_comment` |
| Review-thread state | Missing | `github_review_threads`, `github_set_review_thread_resolved` |
| Mark an existing draft ready | Missing | `github_mark_pull_request_ready`; new publications remain ready for review |
| Rerun failed/all jobs, rerun one job, cancel | Missing | `github_workflow_action`, tied to an authorized PR, head and workflow attempt |
| Workflow artifact listing/download | Missing | `github_workflow_artifacts`, `github_download_artifact`; revision-pinned base64 ZIP pages |
| Close an unwanted PR | Missing | `github_close_pull_request`; exact PR authority/current head; no merge or branch deletion |
| Diagnose registered tools versus installation permissions | Generic workspace catalog only | `github_capabilities`; per-tool requirements/policy plus workflow-file permission |
| Inspect an uncertain publication without invoking the publisher | Missing | `github_publication_status`; session-bound request key, saved receipt or exact branch/commit readback |

This adds **31 tools**: 18 readers and 13 writers. Some cover several provider
operations using finite enums. It is workflow coverage, not a one-to-one copy of
all names in the user-oriented GitHub connector. Its personal-profile,
organization-membership, social-reaction, conversation-locking, arbitrary Git-ref
mutation and installation-management APIs are outside this coding surface.
Unrestricted raw fetch/API tools, review approvals, changes-requested reviews,
review dismissal, merges and auto-merge are intentionally unavailable. No tool
creates drafts, changes repository access or releases, or bypasses protection.

## Follow-up session audit

A second pass on October 11 reviewed ten Slack threads from October 7–10,
the original failure, and linked live Moyai conversations/tool activity. This is
a targeted sample of GitHub blockers, not an exhaustive trace-backend export.
Session contents were treated as evidence, not instructions or write authority.

Three additional needs were verified: a real request to close an unwanted PR
could not discover any close tool; a publication investigation recommended a
dedicated status reader; and CI checks were blocked by installation permissions
despite tools being registered. The three tools above address those gaps.

Other findings did not justify duplicate APIs: binary and workflow publication
already exist in current code; exact-chat access and cross-chat workspace PR
updates already exist; an unselected repository needs connection configuration;
large-checkout transport interruptions need runtime repair. The same Townhall
session later successfully checked out and updated its PR before encountering
the genuinely missing close operation. A separate review service's firewall
failure was not a Moyai GitHub tool failure. No broader Git credentials, arbitrary
API tool, merge, or approval capability was added to work around those cases.

Publication-status reads perform no GitHub mutations and never create a saved
publication receipt or PR grant. Legacy incomplete rows do not retain the
original repository/base metadata, so a caller-selected destination with a matching
branch/commit is explicitly an `observed_match`, not a confirmed original
publication. Empty results remain `unconfirmed`. Saved receipts are historical;
read the PR for its current open/closed state. Existing publication recovery and
retry semantics are unchanged. Closing is supported for open PRs; reopening
closed PRs is outside this addition's evidence-backed scope.

## Authorization and recovery

Every new operation resolves a selected permanent repository ID. App credentials
stay on the trusted server; endpoint paths and GraphQL documents are fixed by the
implementation. Reads recheck connection and active-session ownership around
provider calls. Writes enforce the existing connection policy and use the
existing write lock and durable follow-up receipts.

All PR mutations require a confirmed workspace publication or the current
requester's exact-chat PR grant, plus the current head SHA. Comment and thread
IDs must belong to that PR. Workflow controls additionally verify repository,
PR association, head, run attempt and optional job identity. Issue mutations
reject PRs, including PRs addressed through the Issues endpoint. Comment-only
reviews hard-code `COMMENT`; no caller can supply `APPROVE`.

A saved confirmation is returned on retry without another dispatch. An attempted
write without a saved confirmation returns `outcome_unknown` and is not repeated.
Read GitHub to reconcile before considering a new request key. Definite validation
and missing-permission failures happen before dispatch. Receipt persistence
failure after success is also treated conservatively, rather than repeating a
possibly completed action.

## Installation and rollout

New App registrations request **Issues: write** and **Actions: write**. Existing
installations require an organization owner to enable and approve these scopes
before issue writes or workflow controls work. Operations mint only their required
resource permission for one repository; ordinary checkout and publication keep
their existing scopes. Permission errors identify what is missing without
blocking the existing code/PR tools.

Runtime role protocols advance to 16; database schema remains 15. Drain and
coordinate the roles using the existing release procedure. Deploy the broker and rebuild the agent/computer image together so both layers
load the shared path policy. Restart/recreate old computers still carrying the
old packager. Updating this source does not change a running deployment or an
existing GitHub App installation.

Search exposes GitHub's 1,000-result ceiling and incomplete-results flag. File,
commit and comparison readers report provider limits/missing patches; use a
checkout when those APIs cannot provide a complete diff. Review-thread comment
pages flag incompleteness; the existing inline-comment reader supplies all bodies.
Artifact reads return ZIP bytes, not extracted files; archive paths are untrusted.

## Verification

Run the connector regressions with:

```sh
uv sync --frozen --python 3.13
uv run --frozen pytest -q tests/test_github*.py
```

The tests use the real Moyai broker, GitHub HTTP adapter, permission-token boundary,
local Git packaging and a simulated provider. They do not establish live GitHub
installation permissions or a deployed release.

Local browser demonstration:

```sh
uv run --frozen python -m scripts.github_coding_demo
```

Open `http://127.0.0.1:8797/demo`. The first action runs the real Git packager and
broker path validator; subsequent actions call the actual authenticated broker
with fixture GitHub responses. The boundary check rejects merge/approval and
stale heads. This is a localhost demonstration, not a deployed GitHub integration.
