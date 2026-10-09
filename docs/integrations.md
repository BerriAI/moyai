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
| Linear | Personal API key, or OAuth app with `read,write`; callback `PUBLIC_URL/oauth/linear/callback`. | List accessible teams (50 results), search issue titles (20 results), read an issue and its parent, create an issue (optionally under a parent) directly, update an existing issue's parent or add a comment directly. Creating issues requires the credential’s Create issues permission (Linear also permits updates under that scope). |
| Slack | User token with `search:read` and relevant channel/DM history scopes for reads. Sending requires the installed **Moyai bot** with `chat:write`, `im:write` (to open DMs), and `im:history` (to verify them). OAuth callback: `PUBLIC_URL/oauth/slack/callback`. Bot tokens cannot search messages. | Search messages (20 results), read a thread (50 messages), send as the Moyai app. |
| Notion | Integration token with content access and the target pages shared to it; or public integration OAuth client with callback `PUBLIC_URL/oauth/notion/callback`. | Search page titles (20 results), read up to 100 top-level blocks, append a paragraph directly. |

Set `LINEAR_CLIENT_ID` / `LINEAR_CLIENT_SECRET`, `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET`, and/or `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` to show native OAuth buttons. Provider administrators may need to approve the apps and scopes. Notion search is title search, not full-text search; nested page blocks and subsequent result pages are not automatically expanded in this MVP.

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
Both accept a Linear identifier or UUID. This sends `issueUpdate` with only
`parentId`; it preserves the existing issue instead of creating a replacement or
substituting cross-links. To remove a parent, explicitly pass `"parent_id":null`.
Omitting `parent_id` is rejected. Parent updates execute directly when the
connection allows writes, without an administrator approval step.

Read the ticket with `linear_issue` to verify its returned `parent` (ID,
identifier, title and URL). An update is confirmed only when Linear returns
success, the same issue ID, and the requested parent. Unconfirmed writes are
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

## Shared organization GitHub

Use **Connections → GitHub → Connect** as an administrator. Connect an existing organization-owned App using its App ID and PEM private key, or enter your organization and register a new App. Moyai verifies the numeric organization and installation IDs and encrypts the signing key. Install the App with Contents and Pull requests write access and Metadata read. New registrations also request Administration write for ruleset reviewer edits. Existing Apps can retain narrower permissions; tokens are narrowed to each operation.

Use **Manage → Choose repositories** to select the repositories Moyai can access. Discovery reads GitHub installation metadata; the selected numeric IDs are stored in Moyai’s encrypted connection, independently of Render or the model gateway. Adding a repository may require granting access in GitHub App settings first, then refreshing the picker. Reconnect/check refreshes metadata directly when the App is already installed; no disabled GitHub “Update access” button is involved. Updating the installation never silently expands an existing Moyai selection.

`github_repositories` returns `{id, full_name}` entries. Pass `id` as `repository_id` to checkout, repository, PR, or ruleset tools. Names remain accepted as a compatibility input and resolve only among selected IDs and verified aliases. Tokens use `repository_ids`, API operations use `/repositories/{id}`, and Git checkout metadata records the ID. The Git broker derives the current canonical Git URL from that ID. Name changes update labels without changing authorization or PR ownership. Ambiguous reused names require an explicit ID; a transfer to another organization is rejected.

Existing name-based connections migrate on first use or connection check, preserving their exact selected subset after GitHub verification. Saved sessions, GitHub environment recipes, automation repository references/event filters, and completed PR receipts are backfilled. Public recipes and runs acquire an ID before use. Old unresolved references fail closed until corrected. Legacy publication attempts without a confirmed result are intentionally not recovered across this migration; inspect their branch/PR before publishing with a new request key. Removing a repository revokes broker access and changes the authorization version. Neither `GITHUB_REPOSITORY` nor `GITHUB_REPOSITORIES` is read as runtime configuration; remove obsolete Render entries after rollout.

For a local walkthrough, run `uv run python scripts/github_identity_demo.py` and open `http://127.0.0.1:8796/demo`. It exercises the actual Moyai broker and repository picker against a local GitHub HTTP fixture: migrate a saved name, rename the repository, read the same ID, and reject a different repository that reuses the old name. The demo uses a temporary database and no live GitHub credentials or writes. See the [configuration audit and rollout notes](render-configuration-audit.md).

Public-only runs and environment builds also resolve a permanent ID before cloning. Without a GitHub connection, these metadata lookups use GitHub's unauthenticated API and share its IP-based rate limit. Shared GitHub operations use installation tokens.

Moyai can propose changes to `BerriAI/moyai` through the same branch/PR tool, without an administrator approval step. It cannot merge or deploy its own changes; a human must review and merge, and deployment remains a separate administrative action.

The agent can read repository details and PRs, check out private code, and publish up to 100 changed UTF-8 text files (10 MiB each, 20 MiB total) **without an administrator approval step** when the task requests a PR and the connection permits writes. Publishing creates a unique `moyai/...` branch and a normal, ready-for-review PR. Local commits, uncommitted edits, new nonignored files and deletions are compared against the recorded checkout base. Busy default branches are allowed when the recorded checkout base remains an ancestor. Existing files are never overwritten by checkout.

GitHub's `pull_requests:write` permission includes review/merge capabilities, so GitHub scopes alone cannot restrict agents to publishing and commenting. Moyai enforces this boundary in its server broker: it exposes no approval, review, merge, auto-merge, arbitrary branch update, force-push, or generic GitHub API operation. Signing keys are encrypted on the server; short-lived installation tokens are narrowed to exactly one requested repository and never sent to Modal. Git transport is a streaming read-only `git-upload-pack` endpoint, authenticated with the current session capability. Credentials are not stored in Git URLs/config or command arguments. Workflow, access-control, credential, binary, symlink and submodule changes are rejected; the base tree is checked to prevent implicit directory deletion. Do not add the App as a branch-protection/ruleset bypass actor.

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

A successful publication fetches its commit through read-only Git and advances the local comparison base without changing the working files or index. If synchronization fails, the successful receipt includes recovery guidance. To resume from a missing or stale checkout, use `github_checkout` with `number` and a fresh directory. Existing directories and pending edits are preserved. Large files are uploaded as individual Git blobs; trees contain blob SHAs. Sandbox, server, relay and encrypted tool-call envelopes share compatible byte budgets, including JSON escaping; model and other HTTP requests retain their existing limits.

Publication and follow-up journals use the session plus `request_key` to recover uncertain results across chat turns. Comment recovery searches for a hidden receipt marker and never automatically re-posts an unconfirmed comment. Explicit retries with unchanged files and fields find the existing PR; conflicting payloads, changed installations and externally changed branches stop instead of overwriting. Retrying PR creation does not require approval; read-only and disabled connections still block publication. Pausing/disconnecting GitHub or stopping the session revokes further calls; an already-sent GitHub action cannot be recalled.


**Personal Slack token reads:** Start your own private web chat, enable Slack for the session and save a personal secret using `provider=generic`, `name=slack-personal`, `format=env`, with `SLACK_USER_TOKEN` containing a Slack user OAuth token (`xoxp-`). Save the token in Settings → Secrets before requesting access; never paste tokens into a chat. Private chats can bind an existing personal secret but cannot open credential forms or look up organization vaults. Choose **Personal** sharing and either session-only or persistent reuse. Obtain a request handle through `credentials_request` before reading.

- `slack_personal_search(request_id, query, page=1)` searches 20 messages per page, including private conversations accessible to the token.
- `slack_personal_conversations(request_id, cursor="")` lists up to 100 of the user's conversations, including DMs/group DMs.
- `slack_personal_history(request_id, channel, cursor="")` reads up to 15 recent messages.
- `slack_personal_thread(request_id, channel, thread_ts, cursor="")` reads up to 15 thread messages.

Search returns Slack's paging metadata; the other tools return `response_metadata.next_cursor`. Follow those fields for additional pages. Grant `search:read`, the applicable `channels:read`, `groups:read`, `im:read`, `mpim:read` scopes for listing, and `channels:history`, `groups:history`, `im:history`, `mpim:history` for reading. Slack membership and API rate limits still apply. These tools only make read requests to fixed Slack API endpoints; they never send as the user or fall back to organization credentials. The encrypted token stays server-side. Each read checks the active requester, secret lifetime/revocation, session Slack enablement, and organization Slack pause policy, including again before releasing the response. They work without a shared Slack read token in durable chat sessions.

These tools enforce the same private web chat boundary as personal OAuth: no shared chats, Slack mirrors, automations, or delegated agents. Access and the active turn are checked again before returning results. They are separate from OAuth-backed `slack_search`/`slack_thread` and never fall back to OAuth or organization credentials. Invalid tokens must be replaced through Secrets.
