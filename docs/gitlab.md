# GitLab connections and native workflows

Moyai supports selected GitLab projects for repository inspection, checkout,
feature-branch publication, merge request creation and follow-up, and reviews.
This guide describes the implemented native tools and their limits; GitLab and
GitHub do not have identical APIs or workflow capabilities.

## Connect selected projects

1. Create an active **personal access token with the `api` scope** on the GitLab
   instance you want to connect. Its account must have **Developer or higher**
   access to each selected project. Projects must be unarchived, with repositories
   and merge requests enabled. These setup checks also apply when you choose
   Moyai's read-only policy; `read_api` alone is insufficient.
2. As a Moyai organization admin, open **Settings → Connections**, choose
   **GitLab**, and select **Connect**. Enter the **GitLab URL** and token.
   Use `https://gitlab.com` or the HTTPS origin of a self-hosted instance,
   such as `https://gitlab.example.com`. Enter the origin only, without a project
   path or `/api/v4`.
3. Select **Discover projects**, search or load more as needed, check each project
   you want to allow, and select **Save selected projects**. Discovery does not
   grant access to every project visible to the token. Select at most 200 projects.
4. Set **Available to organization sessions** and choose **Read only** or
   **Read and write** under **Allowed actions**. Select the appropriate project
   connection in the session. Writes require a current selected grant with write
   access; the token's own permissions do not bypass Moyai's policy.

Use **Choose projects** to change the allowlist, **Reconnect** to replace
credentials, and **Check connection** to verify them. Changing project selection
or connection policy invalidates stale grants. Pausing or disconnecting blocks
subsequent authorized dispatch; it cannot recall an action already dispatched.

Credentials are encrypted in server storage. Authenticated provider calls and
Git fetch/push run on the trusted server or worker. Checkout transfers a
credential-free repository bundle to the sandbox; tokens are not placed in the
model context, sandbox environment, repository remotes, or publication receipts.
The selected identity includes the provider, canonical origin and immutable
GitLab project ID. A project path alone does not establish authority.
MR references use the project-local `!iid`; that number is not a global MR or
project ID.

Self-hosted instances must resolve to permitted public network addresses.
Private-network HTTPS hosts, redirects, credentials embedded in URLs, and
instance subpaths are unsupported. Requests remain bound to the configured
origin; an MR link cannot redirect credentials elsewhere. A private project
requires its selected connection, including when opening the review viewer;
there is no anonymous fallback.

## Supported native workflow

| Area | Available behavior | Boundary |
| --- | --- | --- |
| Repository reads | Project metadata, tree, files, branches, commits, comparisons and project-scoped search | Responses are bounded and expose pagination or truncation. Search availability depends on the GitLab server. |
| Issues | List and read issues and their comments | No native issue creation or editing tools. |
| Checkout and editing | Check out a branch, exact commit or same-project MR into a fresh directory under `/workspace`; edit using the session's ordinary coding tools | Provider credentials remain on the trusted worker. Cross-project/fork MR checkout and publication are unsupported. |
| Publication | Create a feature branch, publish explicit files or a staged directory, and create a ready-for-review MR; open an MR from an existing exact head | Binary files, executable modes and deletions are supported. Opening an existing branch's MR does not confer ownership of that branch. |
| Follow-up | Update the same MR, publish a prepared rebase, close, reopen, retarget, or convert to draft when explicitly requested | Requires exact head/base checks and appropriate ownership or requester consent. A prepared rebase publishes one prepared commit on the current target base. |
| Reviews | General or inline comments, pending draft notes, individual submission, and capability-dependent request-changes decisions | See pending-review handling below. Approval decisions are forbidden. |
| Managed stacks | Create, append, list and read ordered same-project MR chains | Moyai maintains the chain locally. This is not GitHub native stack identity or atomic multi-ref updates. |
| CI inspection | Pipelines, jobs, bounded logs, artifact metadata and exact-path artifact downloads | Downloads are limited to 2 MiB. Native CI trigger, retry and cancel tools are not exposed. |
| Review UI | Attach or mention an MR URL, inspect changes, discussion, commits and head-associated pipeline status in the existing panel | Nested group paths are supported. Missing or limited provider data is disclosed rather than inferred. |
| Slack and attribution | Recognize MR links and show a **View MR** card; attribute creation from verified durable receipts | A mentioned or attached URL does not count as a newly created MR. |

For example, ask Moyai to inspect a selected project, check out its default
branch, make and test a change, and publish a dedicated feature branch as a
ready-for-review MR. Follow-up changes can update that MR after a fresh head
check. This is an example workflow, not a recorded verification result.

Bare repository bootstrap is available to chats and automations with the exact
selected GitLab grant. Repository reads, checkout, editing, MR creation and
follow-up publication do not depend on reusable environments.

Existing validated **Modal environment builds** can separately be consumed
through workspace restoration. Selection requires an explicitly resolved
repository URL and `gitlab_project_id`; choosing an environment alone does not
discover its GitLab project. The original and resolved recipes must agree on
the GitLab provider, canonical origin, immutable project ID and repository path,
with a validated snapshot and exact commit. Automatic selection requires a
matching project; it cannot inherit an unrelated default environment.
The exact current GitLab grant is required even for an image marked
`clone_access="public"`.

This provides consumption of already validated images. There is no reusable
environment build-authoring service in this checkout for either provider.
Standalone native connection setup also does not configure GitLab: use the
canonical organization connection above. This restriction does **not** mean
that the product's single-container `standalone` deployment topology is
unsupported.

## Write and review safeguards

Moyai does not expose GitLab merge, approval, branch deletion, arbitrary API or
quick-action execution. It refuses direct publication to the default branch or
a protected branch, including names covered by wildcard protection rules.
Incomplete protection information fails closed. An MR may target the default
branch for review; publishing its feature branch does not merge into the target.

Writes recheck the selected project grant and exact source/target state. Updates
to an external MR need direct requester consent for content changes at the
acknowledged head; lifecycle consent is separate. Delegated agents and automations
cannot grant that consent. If the branch moves, refresh and reauthorize as required.
Trusted Git publication uses an exact-ref lease. GitLab's per-file
`last_commit_id` is not treated as an equivalent whole-branch compare-and-swap,
and there is no GitHub GraphQL `updateRefs` equivalent in this workflow.

Git HTTPS publication and request-changes GraphQL address projects by path.
Numeric project-ID checks before and after dispatch, and exact-ref leases, do
not atomically bind that path to the selected project. With a broad personal
access token authorized to write another project, concurrent project renaming
and path reuse can direct a write to that other project. A post-write identity
check cannot undo a write that has already occurred.

Publication and review operations record durable request keys and individual
acknowledgments. If a write times out or has an unknown outcome, inspect
`publication_status` with the **original request key**. Do not repeat the operation
under a new key to bypass the barrier. A partial result can contain successful
steps even when the overall operation did not complete.

Pending reviews consist of individually tracked GitLab draft notes. Creation,
editing, deletion and submission are restricted to notes acknowledged for the
current actor/session/turn, at the recorded MR head. Moyai never bulk-publishes
all draft notes belonging to the token's account, and pending-review deletion
cannot delete already submitted comments. Multi-note submission can partially
complete; inspect its per-note receipts before continuing.

`REQUEST_CHANGES` requires the deployed GitLab GraphQL schema to expose
`mergeRequestRequestChanges`, plus permission to perform the operation.
Use the native `capabilities` tool to inspect the server capability; permission
is checked at dispatch. Unsupported servers receive an explicit refusal.
A comment is never presented as a substitute request-changes decision.
No universal GitLab version or subscription-tier guarantee is made.

GitLab's request-changes mutation has no atomic expected-head precondition.
Moyai checks the head before dispatch and validates the returned MR identity
and head. The head can change during the request; a returned-head mismatch
causes a failure with the partial receipt retained for inspection. Notes or the
decision may already have been applied. This is not an atomic exact-head review
decision guarantee.

## Local loopback testing

The default origin policy requires HTTPS. For an isolated local GitLab test
instance, explicitly set **`GITLAB_ALLOW_LOOPBACK_HTTP=true`** in the Moyai server
and worker configuration that loads the GitLab adapter. Then use a literal
loopback origin, for example `http://127.0.0.1:58990`.

This opt-in does not allow arbitrary HTTP hosts, `localhost` hostnames, or private
remote addresses. Loopback refers to the network namespace of the process making
the request, so a GitLab instance on the host is not automatically reachable
through a container's loopback address. Keep the normal Moyai authentication and
project-selection checks enabled.

## Implementation references

The current source, rather than provider-wide feature lists, defines this guide:

- [`GitLab.verify`](../app/gitlab.py) and
  [`save_if_unchanged`](../app/connection_authority.py): token verification and
  encrypted credential storage.
- [`normalize_base_url` / `GitLabTransport`](../app/gitlab_transport.py) and
  [`Settings.gitlab_allow_loopback_http`](../app/config.py): origin restrictions.
- [`ProductGitLabSetup`](../native/web/product-gitlab-settings.tsx) and
  [`ProductConnections`](../native/src/moyai/product_connections.py): setup,
  selection and current grants.
- [`toolset`](../native/src/moyai/product_gitlab.py): exposed native capabilities.
- [`require_writable`](../native/src/moyai/gitlab_branch_policy.py),
  [`Publications`](../native/src/moyai/product_gitlab_publication.py) and
  [`Workflows`](../native/src/moyai/product_gitlab_workflows.py): write fences,
  ownership and durable receipts.
- [`Reviews` / `review_capability`](../native/src/moyai/product_gitlab_reviews.py):
  pending notes and real request-changes decisions.
- [`Stacks`](../native/src/moyai/product_gitlab_stacks.py) and
  [`prepare_repository`](../native/src/moyai/product_gitlab_bootstrap.py): managed
  chains and repository bootstrap.
- [`gitlab_recipe_identity`, `gitlab_build_identity` and
  `ProductWorkspaceCompatibility`](../native/src/moyai/product_continuation_workspace.py):
  validated environment selection and current project authority;
  [`ProductWorkspaceRestorer`](../native/src/moyai/product_continuation_restore.py):
  shared image consumption.
