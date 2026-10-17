# GitHub workflows in the Pydantic core

The selected organization GitHub connection exposes scoped native Pydantic tools.
Discover their current schemas through the normal connection catalog. Names in a
conversation have a `github_<connection>_` prefix.

| Task | Tool |
| --- | --- |
| Check out a branch, exact commit, or PR in a fresh directory | `checkout` |
| Publish files as a regular PR with optional base/head branches | `create_pull_request` |
| Create a named branch at an exact commit | `create_branch` |
| Publish files to a new or acknowledged Moyai-owned branch without a PR | `publish_branch` |
| Open a regular PR from an existing exact head | `open_pull_request` |
| Publish follow-up files | `update_pull_request` |
| Replace an authorized feature head with one prepared commit on its current base | `rebase_pull_request` |
| Close, reopen, retarget, or explicitly convert a PR to draft | `close_pull_request`, `reopen_pull_request`, `retarget_pull_request`, `convert_pull_request_to_draft` |
| Prepare or submit a non-approval review | `create_review` |
| Submit, edit, or delete this task's pending review | `submit_review`, `edit_pending_review`, `delete_pending_review` |
| Inspect, create, or extend a GitHub stack | `stacks`, `stack`, `create_stack`, `add_to_stack` |
| Inspect an operation without repeating it | `publication_status` |

PR creation always uses `draft=false`. Creating a PR over an existing branch
does not establish branch ownership. Branch writes require acknowledged Moyai
creation with a verified commit lineage, or the existing explicit requester
consent for this task and exact PR head. Default and protected branches are
rejected. Ref expressions and filesystem traversal are rejected before checkout.
GitHub credentials remain on the trusted worker; only a Git bundle reaches the
computer, and an existing checkout is never replaced.

`request_pull_request_write_access` grants content access to an exact current
head. `request_pr_lifecycle_access` separately asks for metadata access to a
specific unmerged PR during the current task, including commits arriving after
approval. GitHub's close/retarget/draft APIs have no atomic expected-head
precondition, so content consent cannot substitute for lifecycle consent.
Retargeting revokes external-PR grants. A confirmed Moyai-authored PR retains its
creation receipt while its live authorized base follows successful retargets.

Rebase accepts the complete prepared file changes relative to the current base,
creates one commit, and uses GitHub's atomic `updateRefs` `beforeOid` condition.
It does not transplant an arbitrary local commit series.

Every write uses the existing native `ProductGitOperation` and `Effect` records.
An operation key binds the actor, session, selected connection revision,
repository and exact input. Individual effects are claimed under a database row
lock before dispatch. Ambiguous effects are never automatically repeated.
Saved acknowledgments can finish interrupted operations. A branch merely
appearing in readback is not proof that Moyai created it; an existing PR likewise
does not establish new PR authorship.

No tool permits review approval, merging, auto-merge, merge queue actions,
arbitrary credential-bearing API calls, or unconditional forced ref updates.
Unstack is intentionally unavailable: the provider's whole-stack mutation
cannot atomically bind its membership to the PRs the requester authorized.
The non-destructive stack tools validate every affected PR independently.

## Upgrade and validation

Native protocol revision 4 and the `product-github-workflows-v17` execution receipt
require the supported coordinated upgrade procedure. The receipt adds no table
and preserves every earlier schema receipt. Do not deploy an old worker against
new workflow histories. This PR does not upgrade or deploy production.

From the repository root, with disposable local PostgreSQL:

```sh
uv sync --frozen
MOYAI_TEST_POSTGRES=postgresql://localhost/postgres uv run --frozen pytest -q \
  native/tests/test_product_github_publication.py \
  native/tests/test_product_github_workflows.py \
  native/tests/test_product_github_bootstrap.py \
  native/tests/test_product_connections.py \
  native/tests/test_product_execution_version.py \
  native/tests/test_product_schema_versions.py
uv run --frozen --directory native pyright
uv run --frozen --directory native ruff check src tests
```

These tests use the real native SQL/effect machinery and provider protocol
fixtures. They are supplementary to actual application E2E.

For live acceptance, run baseline and final revisions through the supported
local existing-product deployment with isolated stores and the scoped E2E
launcher. Configure the approved GitHub App for a selected test repository and
a real development model. In the real chat, check out a selected ref, publish a
new disposable branch without a PR, open its chosen-base ready-for-review PR,
and inspect its actual author, refs and contents on GitHub. Exercise follow-up
publication and an expected-head rebase, then close only the disposable PRs.
Record the same request on baseline and final, with exact commit IDs, and capture
the actual application/service interaction. Never use a fixture demo or replayed
test output as this proof.
