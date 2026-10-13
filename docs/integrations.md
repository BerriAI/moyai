# App connections

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Connect the apps

New browser sessions select all enabled connected apps by default; uncheck an app to exclude it from that session. No per-user provider sign-in is required.

Connections are **shared by the LiteLLM organization** and retain the permissions of their authorizing identity. The deployed app uses verified BerriAI Google SSO; password sign-in is a configurable fallback. Members can start sessions and use all enabled connected-app tools without an administrator approval step. Only admins can manage connections, change access policies, view spend or link Slack identities. This is a single-organization trusted-team MVP with shared session visibility.

Open **Organization** and either enter the appropriate token or use the OAuth button after configuring the provider's client ID and secret. Tokens are checked with the provider before being saved. Disconnect removes the locally stored credential; revoke the integration at the provider as well if you want to terminate its authorization there.

| App | Required setup | Tools exposed |
| --- | --- | --- |
| Linear | Personal API key, or OAuth app with `read,write`; callback `PUBLIC_URL/oauth/linear/callback`. | Page through teams, requester issues, title search, and issue comments; read descriptions and parents; create, reparent, or comment on issues. Creating issues requires the credential’s Create issues permission (Linear also permits updates under that scope). |
| Slack | User token with `search:read` and relevant channel/DM history scopes for reads. Sending requires the installed **Moyai bot** with `chat:write`, `im:write` (to open DMs), and `im:history` (to verify them). OAuth callback: `PUBLIC_URL/oauth/slack/callback`. Bot tokens cannot search messages. | Page through search matches, channel roots, or thread replies; send as the Moyai app. |
| Notion | Integration token with content access and the target pages shared to it; or public integration OAuth client with callback `PUBLIC_URL/oauth/notion/callback`. | Page through title search and page/block children, traverse nested blocks, and append complete text. |

Set `LINEAR_CLIENT_ID` / `LINEAR_CLIENT_SECRET`, `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`, and/or `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` to show native OAuth buttons. Provider administrators may need to approve the apps and scopes. Notion search is title search, not full-text search.

Research reads return the complete provider page plus `next_arguments` and
`coverage`. Call the same tool with `next_arguments` until it is null, and check
the coverage warning: a missing or repeated provider cursor is an incomplete
result. `page_size` changes transport size, not total reachable results. Provider
permissions, retention, search indexing, rate limits, and concurrent edits still
bound coverage; these tools do not claim to mirror an entire workspace.
`notion_page` accepts a page or block ID; traverse its `child_collections` to read
nested blocks. `slack_channel` reads roots, with replies through `slack_thread`.

Long Slack sends continue in replies at Slack's 40,000-character field boundary,
returning receipts for every part. Notion append divides text across rich-text
values and payloads at Notion's actual API limits. Neither discards the remaining
text. Partial failures report acknowledged parts and the completed offset. Verify
the failed part before continuing; resending the entire original would duplicate
earlier parts. Each batch rechecks connection policy and execution ownership.

Connector tool errors report `code`, `outcome`, `dispatched`, `retryable`, and
when available `status_code` and `retry_after_seconds`. Invalid arguments produce
field paths/types without copying submitted values. Rejections that occur before
dispatch can be corrected; HTTP rate limits may be retried after the indicated
delay. A lost write response has an unknown outcome and must be reconciled at its
destination first. Authentication and expired session capabilities still reject
the HTTP request. Legacy GitHub errors without dispatch evidence remain unknown
for writes; no automatic write replay is inferred from a generic failure.

**Slack sender identity:** `slack_send` always uses the installed bot, including in shared chats and follow-up turns from other users. The server prefixes the body with the active requester's stored profile name (`Moe: message` or `Tin: message`), not the session creator or connection owner. The agent supplies only the message body and cannot override the sender. Unidentified/shared-password requesters cannot send. It never loads or falls back to the shared user token. Pass a recipient’s Slack user ID (`U…` or `W…`) as `channel` for a DM; Moyai opens the bot’s conversation before posting. Existing bot-accessible channel/conversation IDs still work. Missing, expired, or insufficient bot access fails with a reconnect instruction. Use `slack_thread(as_bot=true)` with the returned channel and timestamp to verify the bot’s DM without switching to the shared read account. Reconnect existing installations to grant `im:write` and `im:history` before opening/verifying bot DMs. New OAuth connections no longer request user `chat:write`; previously granted user permissions are not automatically revoked. Shared search/thread reads remain unchanged; per-user Slack OAuth is not implemented.

This application registers its own app integrations. It does not reuse or copy credentials from the Codex/ChatGPT connectors in this chat.

Local sender demo: run `uv run python scripts/slack_sender_demo.py` and open `http://127.0.0.1:8796/demo`. It calls the real broker and connector with stored test profiles and a simulated Slack HTTP API, showing consecutive `Moe:` and `Tin:` messages in the bot's DM and a blocked send when the bot is removed. No live Slack messages are sent.

## Linear tickets and sub-issues

When the user requests a ticket, `linear_create_issue` creates it directly, with no administrator approval step for either members or admins. Use `linear_teams` to resolve the intended team. The session must enable Linear, the shared connection must permit writes, and the connected credential must have Create issues permission. Disabled/read-only connections, missing credentials and revoked session capabilities still block creation. If the provider does not confirm the write, verify the destination before retrying. Linear comments, parent updates and other enabled connected-app writes also execute directly.

For a **new** sub-issue, include `parent_id` in `linear_create_issue`. Omit it (or
use `null`) for a standalone issue. The original three required arguments remain
unchanged. A missing or inaccessible parent stops creation.

For an **existing** ticket, use `linear_update_issue` with `issue_id` and
`parent_id`, for example `{"issue_id":"LIT-1234","parent_id":"LIT-9222"}`.
Both accept a Linear identifier or UUID. A parent-only call sends `issueUpdate`
with only `parentId`; it preserves the existing issue instead of creating a replacement or
substituting cross-links. To remove a parent, explicitly pass `"parent_id":null`.
Omitting a field preserves it; supply at least one update field. Updates execute directly when the
connection allows writes, without an administrator approval step.

`linear_update_issue` also accepts `title`, Markdown `description`, `state_id`
(workflow status), `assignee_id`, `priority` (0–4), `label_ids`, `project_id`,
`cycle_id` and `due_date` (`YYYY-MM-DD`). Explicit null clears description,
assignee, parent, project, cycle or due date. Title, state, labels and priority
reject null: use `label_ids: []` to remove all labels and `priority: 0` for no
priority. Labels replace the whole set. For example,
`{"issue_id":"LIT-1234","title":"Corrected title","priority":2}` leaves the
parent, labels, assignee and all other fields unchanged.

Find the issue's team using `linear_issue`, then call
`linear_options(team_id=…, resource="states"|"members"|"labels"|"projects"|"cycles")`.
Each resource has independent cursor pagination; follow `next_arguments`.
Use returned IDs, not ambiguous names. Discovery is scoped to the selected team;
Linear still enforces issue/team permissions and relationship validity.

Read the ticket with `linear_issue` to verify the changed fields and its
returned parent. Comments and labels paginate separately: follow
`next_arguments` for comments and `labels_next_arguments` for labels.
An update is confirmed only when Linear returns
success, the same issue ID, and the supplied field values. Unconfirmed writes are
marked uncertain and are never retried automatically. Reparenting several
existing tickets requires one update per ticket; no new tickets are needed.

Run `uv run python scripts/linear_parenting_demo.py` for a local demonstration
of five existing issues being moved under one parent without approval clicks.
It exercises the broker, connector HTTP handling and parent readback against a
simulated Linear API, with no live provider calls. The script's `--pause 1.5`
option spaces out the output for a terminal recording.

After deployment, these tools are advertised by the workspace broker and its
MCP bridge; a running agent may need to refresh its tool discovery or resume in
a new turn to see the updated schema. The connector uses Linear's
[issue update API](https://linear.app/developers/graphql#creating--editing-issues).

## Notion pages, properties and blocks

Native requests pin **Notion-Version: 2025-09-03**. In this version databases
contain data sources: retrieve a database using
`notion_object(kind="database", object_id=…)`, choose an explicit `data_sources`
entry, then retrieve its schema with `kind="data_source"`. Query rows with
`notion_query(data_source_id=…)`. Do not send a database ID to a data-source
endpoint or silently choose the first source in a multi-source database.

`notion_discover` searches page or data-source titles visible to the integration.
An empty query lists accessible objects of the selected kind. `notion_search`
retains its existing page-title behavior. Neither tool provides workspace-wide
content search. `notion_query` supports typed AND conditions and ordered property
sorts on a single data source; unsupported formula/rollup filters are rejected.
Follow returned continuations for search, queries and property collections.

`notion_object(kind="page")` reads metadata and properties; `notion_property`
reads a property's full paginated values using its returned ID.
`notion_page` still reads direct block children and returns explicit descendant
collections, each with its own cursor.

`notion_create_page` accepts a shared `page_id` or `data_source_id` parent,
typed properties and optional children. Under a page, supply just
`{"property":"title","type":"title","text":"New page"}`. For a data-source row,
use exact property names/IDs and existing options from its schema.
`notion_update_page` modifies only supplied properties or `in_trash`.
Property values replace existing values: `[]` clears multi-select, people or
relations, empty text clears text, and null clears nullable number, select,
status, date and contact values. Read-only calculated properties are not writable.

`notion_append_blocks` appends up to 100 structured blocks, optionally after
an existing sibling, with at most two levels in one call. Supported blocks are
paragraphs, headings, lists, to-dos, toggles, quotes, code and dividers.
Rich-text segments support bold, italic, strikethrough, code and HTTP(S) links.
`notion_update_block` replaces content while preserving the current block type,
or archives/restores via `archived`. Append children separately. The original
`notion_append(page_id, text)` paragraph helper remains supported.
Notion's 2,000-character rich-text, array and 500 KB payload constraints apply.

The connection must have access to the parent, target and relation destinations
and the provider's read/insert/update content capabilities. Local write policies,
session app selection and private-session restrictions apply to every operation.
Read the target with `notion_object`/`notion_property` after an uncertain update.
For uncertain creation or append, search/query/read the parent and reconcile
the returned IDs and content before explicitly retrying. No mutation auto-retries.

## Slack message operations

`slack_reply(channel, thread_ts, text)` verifies an exact thread root through
the installed bot and posts an attributed reply. All parts of a long reply
stay in that original thread. `slack_send` remains compatible, including `me`
and explicit user-ID DM resolution.

`slack_message(channel, ts)` retrieves one exact message and reactions using
the bot. `slack_edit_message` and `slack_delete_message` verify its channel,
timestamp and bot author before mutation. Edit replaces one message's body,
adds the active requester's attribution, and clears old blocks/attachments.
`slack_reaction(action="add"|"remove", channel, ts, name)` operates only on the
bot's own reaction; already-present/absent outcomes are acknowledged.

Reconnect existing Slack installations for **bot `reactions:read`**.
Replies/edits/deletes additionally require `chat:write`; reactions require
`reactions:write`. Bot membership and provider permissions still apply.
These operations never use a personal or shared user token. Existing personal
Slack reads retain their private-session selection and fail-closed boundaries.
Private sessions cannot use these writes.

Reconcile uncertain edits/reactions with `slack_message`; verify reply parts
with `slack_thread(as_bot=true)` and the original root. A not-found/inaccessible
message alone does not prove deletion. Preserve acknowledged parts after a
partial reply, and never resend the full body automatically.

Provider references: [Linear GraphQL](https://linear.app/developers/graphql)
and [official schema](https://github.com/linear/linear/blob/master/packages/sdk/src/schema.graphql);
[Notion version migration](https://developers.notion.com/docs/upgrade-guide-2025-09-03),
[page creation](https://developers.notion.com/reference/post-page),
[page update](https://developers.notion.com/reference/patch-page),
[data-source query](https://developers.notion.com/reference/query-a-data-source);
Slack [reactions.get](https://docs.slack.dev/reference/methods/reactions.get/),
[chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/),
[chat.update](https://docs.slack.dev/reference/methods/chat.update/),
[chat.delete](https://docs.slack.dev/reference/methods/chat.delete/).

## Shared organization GitHub

Use **Connections → GitHub → Connect** as an administrator. Connect an existing organization-owned App using its App ID and PEM private key, or enter your organization and register a new App. Moyai verifies the numeric organization and installation IDs and encrypts the signing key. Install the App with Contents and Pull requests write access and Metadata read. New registrations also request Administration write for ruleset reviewer edits. Existing Apps can retain narrower permissions; tokens are narrowed to each operation.

Use **Manage → Choose repositories** to select the repositories Moyai can access. Discovery reads GitHub installation metadata; the selected numeric IDs are stored in Moyai’s encrypted connection, independently of Render or the model gateway. Adding a repository may require granting access in GitHub App settings first, then refreshing the picker. Reconnect/check refreshes metadata directly when the App is already installed; no disabled GitHub “Update access” button is involved. Updating the installation never silently expands an existing Moyai selection.

`github_repositories` returns `{id, full_name}` entries. Pass `id` as `repository_id` to checkout, repository, PR, or ruleset tools. Names remain accepted as a compatibility input and resolve only among selected IDs and verified aliases. Tokens use `repository_ids`, API operations use `/repositories/{id}`, and Git checkout metadata records the ID. The Git broker derives the current canonical Git URL from that ID. Name changes update labels without changing authorization or PR ownership. Ambiguous reused names require an explicit ID; a transfer to another organization is rejected.

Existing name-based connections migrate on first use or connection check, preserving their exact selected subset after GitHub verification. Saved sessions, GitHub environment recipes, automation repository references/event filters, and completed PR receipts are backfilled. Public recipes and runs acquire an ID before use. Old unresolved references fail closed until corrected. Legacy publication attempts without a confirmed result are intentionally not recovered across this migration; inspect their branch/PR before publishing with a new request key. Removing a repository revokes broker access and changes the authorization version. Neither `GITHUB_REPOSITORY` nor `GITHUB_REPOSITORIES` is read as runtime configuration; remove obsolete Render entries after rollout.

For a local walkthrough, run `uv run python scripts/github_identity_demo.py` and open `http://127.0.0.1:8796/demo`. It exercises the actual Moyai broker and repository picker against a local GitHub HTTP fixture: migrate a saved name, rename the repository, read the same ID, and reject a different repository that reuses the old name. The demo uses a temporary database and no live GitHub credentials or writes. See the [configuration audit and rollout notes](render-configuration-audit.md).

Public-only runs and environment builds also resolve a permanent ID before cloning. Without a GitHub connection, these metadata lookups use GitHub's unauthenticated API and share its IP-based rate limit. Shared GitHub operations use installation tokens.

Moyai can propose changes to `BerriAI/moyai` through the same branch/PR tool, without an administrator approval step. It cannot merge or deploy its own changes; a human must review and merge, and deployment remains a separate administrative action.

The agent can read repository details and PRs, check out private code, and publish complete text and binary file changes **without an administrator approval step** when the task requests a PR and the connection permits writes. Publishing creates a unique `moyai/...` branch and a normal, ready-for-review PR. Local commits, uncommitted edits, new nonignored files and deletions are compared against the recorded checkout base. Busy default branches are allowed when the recorded checkout base remains an ancestor. Existing files are never overwritten by checkout. Publication has no fixed local file-count, per-file or total-byte ceiling. Large requests use encrypted multipart transport; binary files use validated base64 and retain their exact bytes. GitHub's own size and rate constraints still apply, and provider rejection is reported. Existing text publication receipts retain their original identity.

GitHub's `pull_requests:write` permission includes review/merge capabilities, so GitHub scopes alone cannot restrict agents to publishing and commenting. Moyai enforces this boundary in its server broker: it exposes no approval, review, merge, auto-merge, arbitrary branch update, force-push, or generic GitHub API operation. Signing keys are encrypted on the server; short-lived installation tokens are narrowed to exactly one requested repository and never sent to Modal. Git transport is a streaming read-only `git-upload-pack` endpoint, authenticated with the current session capability. Credentials are not stored in Git URLs/config or command arguments. Workflow, access-control, credential, symlink and submodule changes are rejected; the base tree is checked to prevent implicit directory deletion. Do not add the App as a branch-protection/ruleset bypass actor.

### Repository rulesets and automatic reviewers

Use `github_rulesets` (paginate with `next_page`) and `github_ruleset` to inspect
repository and inherited organization rules, including the `pull_request` rule's
`required_reviewers` and file patterns. These tools request only Metadata read;
Administration access is not required to diagnose automatic reviewer requests.
Check rulesets when CODEOWNERS and workflows do not explain the behavior.

`github_update_ruleset_reviewers` replaces only the required-reviewer entries in
one repository-owned branch ruleset. Supply an explicit `repository`,
`ruleset_id`, the `revision` from `github_ruleset`, and the complete desired
`required_reviewers` list (including narrower entries that should remain).
For example, `required_reviewers: []` removes all required team entries from
that one ruleset. Each entry uses GitHub's `reviewer: {id, type: "Team"}`,
`file_patterns` and `minimum_approvals` fields. Other rulesets are unaffected.
The tool preserves the general approval count, code-owner review, status checks,
enforcement, branch conditions and bypass actors. It cannot edit inherited
organization rules, create/delete rulesets, or change other protections.

Editing requires the organization GitHub App **Administration: read and write**
permission. For existing Apps, an organization owner enables that repository
permission in GitHub App settings and approves the installation's pending
permission request. No replacement signing key is needed. Missing permission
produces actionable guidance; inspection and PR operations remain available.
The server mints an administration-only token for reviewer updates, keeps it out
of the sandbox, and rechecks the live session, connection and write policy
before sending the update. Broader App grants never carry over into PR/checkout
tokens. The existing connection's read-only setting blocks reviewer edits too.

A changed revision stops the update. GitHub does not expose an atomic revision
condition here, so this is a preflight conflict check, not a lock against an
external edit between read and write. Only `rules` is sent, and a fresh read
verifies both the requested result and the preserved settings. Unconfirmed
writes are never retried automatically: inspect the current ruleset before an
explicit retry. The `required_reviewers` API is currently a GitHub beta.

Run `uv run python scripts/github_rulesets_demo.py` for a local broker demo, or
add `--serve` and open `http://127.0.0.1:8794` for the browser recording flow.
It exercises the real broker and GitHub HTTP client against an in-memory
provider, removes only the wildcard reviewer entry, verifies preserved rules,
and demonstrates stale-edit rejection. It never changes live GitHub settings.

After publication, `github_update_pull_request` publishes another commit to the same open PR, and `github_comment_pull_request` posts a discussion comment (including a user-requested review-bot command). You can paste a Moyai PR link into a new chat and ask for changes: check out its current head with `github_checkout` using the repository and `number` in a fresh directory, then update the same PR. Both writes require a confirmed publication receipt from any chat in this workspace under the current GitHub connection; a branch name or bot author alone never grants access. Repository selection and connection write policy still apply. Original publication and creator records remain attached to the creating chat; continuing a PR does not create another publication or transfer attribution. The server verifies the head repository/branch, rejects stale bases and uses a non-forced ref update. `github_pull_request_comments` reads discussion, inline comments and review summaries with explicit pagination. These tools do not submit reviews, approvals or merges.

A successful publication fetches its commit through read-only Git and advances the local comparison base without changing the working files or index. If synchronization fails, the successful receipt includes recovery guidance. To resume from a missing or stale checkout, use `github_checkout` with `number` and a fresh directory. Existing directories and pending edits are preserved. Files are uploaded as individual Git blobs; trees contain blob SHAs. Large publication requests travel in authenticated multipart envelopes without a local total-content cutoff. GitHub's own file and API constraints still apply.

Publication and follow-up journals use the session plus `request_key` to recover uncertain results across chat turns. Comment recovery searches for a hidden receipt marker and never automatically re-posts an unconfirmed comment. Explicit retries with unchanged files and fields find the existing PR; conflicting payloads, changed installations and externally changed branches stop instead of overwriting. Retrying PR creation does not require approval; read-only and disabled connections still block publication. Pausing/disconnecting GitHub or stopping the session revokes further calls; an already-sent GitHub action cannot be recalled.


**Personal Slack token reads:** Start your own private web chat, enable Slack for the session and save a personal secret using `provider=generic`, `name=slack-personal`, `format=env`, with `SLACK_USER_TOKEN` containing a Slack user OAuth token (`xoxp-`). Save the token in Settings → Secrets before requesting access; never paste tokens into a chat. Private chats can bind an existing personal secret but cannot open credential forms or look up organization vaults. Choose **Personal** sharing and either session-only or persistent reuse. Obtain a request handle through `credentials_request` before reading.

- `slack_personal_search(request_id, query, page=1)` searches 20 messages per page, including private conversations accessible to the token.
- `slack_personal_conversations(request_id, cursor="")` lists up to 100 of the user's conversations, including DMs/group DMs.
- `slack_personal_history(request_id, channel, cursor="")` reads up to 15 recent messages.
- `slack_personal_thread(request_id, channel, thread_ts, cursor="")` reads up to 15 thread messages.

Search returns Slack's paging metadata; the other tools return `response_metadata.next_cursor`. Follow those fields for additional pages. Grant `search:read`, the applicable `channels:read`, `groups:read`, `im:read`, `mpim:read` scopes for listing, and `channels:history`, `groups:history`, `im:history`, `mpim:history` for reading. Slack membership and API rate limits still apply. These tools only make read requests to fixed Slack API endpoints; they never send as the user or fall back to organization credentials. The encrypted token stays server-side. Each read checks the active requester, secret lifetime/revocation, session Slack enablement, and organization Slack pause policy, including again before releasing the response. They work without a shared Slack read token in durable chat sessions.

These tools enforce the same private web chat boundary as personal OAuth: no shared chats, Slack mirrors, automations, or delegated agents. Access and the active turn are checked again before returning results. They are separate from OAuth-backed `slack_search`/`slack_thread` and never fall back to OAuth or organization credentials. Invalid tokens must be replaced through Secrets.
