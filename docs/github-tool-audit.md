# GitHub coding-tool audit

The October 11 audit below is historical. The October 13 follow-up at the end
extends its fixed publication pipeline with controlled branch/base/head
selection, PR lifecycle and non-approval reviews, native stacks, and a bounded
feature-head rebase. It does not expose PR approval or merging.

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
| Images/recordings in PR descriptions and comments | Revocable hosted links available | Existing `media_list` / `media_share` / `media_revoke`, plus new PR-description edits; native GitHub uploads remain a separate gap |

This adds **31 tools**: 18 readers and 13 writers. Some cover several provider
operations using finite enums. It is workflow coverage, not a one-to-one copy of
all names in the user-oriented GitHub connector. Its personal-profile,
organization-membership, social-reaction, conversation-locking, arbitrary Git-ref
mutation and installation-management APIs are outside this coding surface.
Unrestricted raw fetch/API tools, review approvals, changes-requested reviews,
review dismissal, merges and auto-merge are intentionally unavailable. No tool
creates drafts, changes repository access or releases, or bypasses protection.

`github_search` accepts plain terms and double-quoted phrases separated by
spaces. Apostrophes and Unicode are preserved literally in the outgoing query;
for example, `don't "user's café"` stays intact. Empty terms, unmatched or
adjacent quotes, qualifiers, boolean operators, backslashes and control
characters are rejected before provider access. The broker quotes every term
and appends the selected repository and issue/PR kind itself. All four search
kinds use the same parser and serialization path.

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

## Broader historical pass and remaining gap

A third pass reviewed GitHub-related content in **15 additional Slack threads**
from October 3–8 and **four additional live session histories**. Three live histories
were expanded to inspect recorded GitHub calls; older records sometimes retained
tool names/completion status without inputs or results. Searches also included
October 9–10 and excluded threads already covered by the previous pass. This is
still a targeted sample, not an exhaustive tracing-backend export; no Langfuse
project credentials were available to this audit.

| Historical symptom | Current disposition |
| --- | --- |
| Generated API files rejected above 1 MiB | Already fixed by the existing publisher. Rechecked large text, binary and more-than-100-file regressions. |
| Repository visible on GitHub but publishing denied | Repository selection/connection configuration, not a missing operation. |
| Vercel deployment failed while GitHub code/log reads worked | Separate deployment-provider access. GitHub tools do not expose Vercel's private logs. |
| Actions admin settings denied | Separate administration permission; ordinary CI reads and the new PR-bound rerun/cancel tools do not require granting settings administration. |
| Local fix completed without opening a PR | Agent delivery behavior; subsequent publication succeeded with the existing tool. |
| Review findings required follow-up edits/re-review | Existing file publication and PR comments worked; this PR adds dedicated replies, thread state and reviewer requests. |
| Broken screenshot paths in PR body | PR-description editing is now covered. Existing hosted media tools can produce usable links when sharing is authorized and storage/public-origin prerequisites are configured. |
| Native GitHub image/video attachments requested | **Still missing from Moyai's App connector.** Requires a separate user-authenticated upload integration, not another App installation scope. |

GitHub's current [CLI attachment documentation](https://github.com/cli/cli/blob/trunk/skills/gh/SKILL.md#attaching-images-and-videos)
describes uploads using OAuth/PAT user credentials with repository write access;
the installation-token connection used here is not that upload path. Do not
claim `media_share` produces native GitHub attachments or silently substitute
public hosted links when the user specifically requested native uploads. Native
upload support would need trusted-server credential storage, fixed upload-only
operations, exact PR/session authorization, selected immutable media revisions,
and durable partial-upload receipts. An unrestricted user token in the agent
would undermine the no-merge/no-approval boundary. This PR does not add that
authentication flow or count native uploads among its 31 tools.

## Tool discovery

The added tools join the ordinary broker catalog when GitHub is selected for the
session and the connection policy permits the operation. The MCP bridge advertises
that catalog, including these additions; no per-tool installation or activation
step is required after the coordinated release. Read-only connections hide writers.
Installation scopes and exact-PR authorization are checked separately at execution.

Definitions are **loaded on demand**, not eagerly inserted into every model prompt:
Codex uses native `tool_search` where supported (or its actual catalog/`ALL_TOOLS`),
Claude uses `ToolSearch`, and Hermes uses `tool_search`, `tool_describe`, then
`tool_call`. The Codex runtime waits for the MCP server before its first request.
The native-search integration regression verifies deferred schema discovery,
execution through the real bridge/relay/broker, and hiding an unselected GitHub
connection. Registration in this branch does not imply deployment to existing
sessions or old computer images.

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

## October 13: workflow-level parity follow-up

The reported session began in `BerriAI/agent-shin` and requested publication in
another selected repository. Its source PR was already merged, so ordinary file
publication against the default branch had no new diff. Repository switching
was available; selecting a historical base, publishing a branch without a PR,
and opening a PR from an existing chosen head were not.

Internal use of GitHub branch/commit APIs did not make those workflows available
to the model. This follow-up compares the public developer workflows in
[Devin's GitHub integration](https://docs.devin.ai/integrations/gh),
[native stacks](https://docs.devin.ai/work-with-devin/stacked-prs),
[review workflow](https://docs.devin.ai/work-with-devin/devin-review) and
[stale-PR rebasing](https://docs.devin.ai/automation-templates/stale-pr-cleanup).
Devin's private tool catalog is not public, and its permission inventory is not
evidence of a separately exposed tool for every GitHub API.

| Workflow | Moyai surface |
| --- | --- |
| Switch selected repositories | Existing permanent-ID repository selection; checkout into a fresh directory and pass it on publication |
| Choose a source branch/commit | `github_checkout(ref=...)`, mutually exclusive with a PR number |
| Choose new PR base/head | `github_create_pull_request` accepts `base_branch` and `head_branch`; omission retains default publication |
| Create a named branch at an exact commit | `github_create_branch` |
| Publish files without opening a PR | `github_publish_branch`; its confirmation is not a PR receipt |
| Open a PR from an existing head | `github_open_pull_request`, pinned to the expected head |
| Rebase or squash prepared feature changes | `github_rebase_pull_request`, with atomic expected-head fencing and the same PR identity |
| Reopen or retarget an authorized PR | `github_reopen_pull_request`, `github_retarget_pull_request` |
| Explicitly requested draft conversion | `github_convert_pull_request_to_draft`; creation remains ready for review |
| Comment, request changes, or prepare a pending review | `github_create_review`; existing `github_comment_review` remains COMMENT-only |
| Submit/edit/delete this chat's confirmed pending review | `github_submit_review`, `github_edit_pending_review`, `github_delete_pending_review` |
| Inspect native GitHub stacks | `github_stacks`, `github_stack` |
| Create/add/unstack authorized native stacks | `github_create_stack`, `github_add_to_stack`, `github_unstack` |
| Issues, reviewer requests, comments, CI reads/reruns, artifacts | Existing audited tools remain available under their existing policies |
| Repository PR templates | Read the applicable template through checkout/file tools; bundled coding guidance requires using it |
| GitHub event-driven work | Existing authenticated automation sources cover issues/comments, PRs/reviews, check runs and pushes |
| PR approval, merge, auto-merge, merge queue, stack merge | Unavailable; the broker rejects these effects before provider dispatch |

This adds 16 model-visible operations and extends existing checkout/publication
schemas. It is not a promise of raw Git push, arbitrary local commit-series
replication, native GitHub media uploads, or every administrative capability
listed in another product's installation permissions. A prepared rebase creates
one commit on the selected current base. GitHub-triggered automation is existing
functionality; a provider-specific slash-command UI is not implied by its event
support.

### Authority and durable outcomes

Publication freezes permanent repository ID, connection revision, selected
base/head, source commit and operation kind before effects. Database claims,
not only a process-local lock, prevent competing brokers from dispatching the
same phase. Read-only reconciliation can confirm an attempted effect; missing
confirmation never triggers an automatic repeat.

PR authorship and head-branch ownership are separate. Opening a PR over an
existing head does not make that branch writable. File changes require trusted
branch provenance or the existing requester/session/exact-PR grant, along with
the current head and branch policy. A confirmed retarget invalidates the old
base-scoped grant without changing historical creation provenance.

Protected/default branch checks and the no-approval/no-merge policy remain
independent of repository selection, admin identity and App token permissions.
No App credential enters the sandbox. The low-level GitHub client accepts only
fixed mutation paths and exact GraphQL documents. The explicit rebase workflow
uses GitHub's atomic `beforeOid` contract; it does not offer an unconditional
force flag or a target-branch write shortcut.

Native stack operations authorize and recheck every affected PR and the
membership snapshot. A grant for one member is not authority over the rest.
Unstack can leave locked members: HTTP 200 is not automatically a dissolved
stack, while the documented 204 result represents dissolution. No operation
clears merge-queue blockers or invokes stack merge.

### Discovery and verification boundary

The default Deep Agents integration retains its two model-facing functions,
`workspace_tools` and `workspace_call`; catalog discovery returns the actual
operation schemas. A combined-connection request-builder regression covers
all additions without truncation or reordering. Catalog byte sizes are not
token counts or evidence of a provider context limit.

Required live proof starts with the real local baseline and final applications,
using isolated PostgreSQL/Temporal and the actual API, coordinator, broker and
worker roles. The test begins in `agent-shin`, switches to `moyai`, publishes
without a PR, then opens the chosen base/head PR and verifies its actual
repository, author, refs, contents and ready-for-review status. Capture the real
UI or service workflow. Provider fixtures and schema tests remain supplementary.
Never send a real approval/merge request merely to test rejection.

The schema advances to revision 20 and all runtime-role protocols to 19. Stop incompatible runtime owners and use
the supported migration/release procedure; rebuild the sandbox runtime so
packager code, shared ref validation and bundled guidance agree with the broker.
Changing this branch does not deploy production, alter an App installation's
permissions or upgrade an existing computer automatically.
