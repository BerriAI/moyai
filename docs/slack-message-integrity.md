# Slack messages in Moyai

Slack message provenance requires native application schema **v21** (`2026-10-18-message-sources-v21`). Deployments on an earlier schema must use the coordinated upgrade procedure below. The execution authorization cleanup does not require another schema migration or a Slack reconnect.

A Slack message displays its Slack icon, sender name, verified **You** label, timestamp and source link above the authored text. Slack thread references and file inspection text remain encrypted execution input. They do not become the visible message, search snippet or personal-memory evidence. A matching email or an analytics identity alone never establishes **You** or authorizes an action.

## Adding another event source

Both Slack entry points build an `InboundEvent` after verifying the sender and source. It carries the source name, admitted sender ID, stable message identity, authored text, model context, attachment references and encrypted provenance. `NewMessage.from_event` feeds the existing transactional admission path, which checks sender/payload consistency and retains its authorization, attachment ownership and duplicate-request checks. Slack receipt keys stay unchanged.

A future GitHub adapter can use this contract after verifying its webhook, mapping the sender to an authorized actor and checking repository access. It must supply a stable message identity rather than a delivery-attempt ID. Provider permission checks, conversation routing, context hydration and outbound delivery remain adapter responsibilities. Public provenance requires an explicit display whitelist. This change includes a synthetic GitHub event admission test; it does not install a GitHub webhook or grant repository access.

## Coverage and behavior

| Touch point | Behavior in this change |
| --- | --- |
| Signed intake | Stores authored text, encrypted source metadata and frozen model input separately. Browser JSON cannot set trusted provenance. |
| Names and controls | Uses the current verified Slack-to-sign-in relationship for self labels, archive/rename, queued input controls, requester activity and effects. The coordinator refreshes native Slack senders in bounded batches while chats are idle. Execution retains its original authority. |
| Conversation audience | Browser reads and new input check the Slack installation and current caller. DMs require the verified requester; private channels require current membership. Public channels still require a verified workspace identity. External shared channels and conversations whose audience cannot be verified remain unavailable. |
| Execution authority | Admitted work revalidates its saved product identity, selected connections and task state without repeating Slack membership API calls during preparation or each tool operation. Slack installation, sender and selected-account revocation still stop execution. Removing channel membership prevents subsequent browser reads and new input; it does not retroactively cancel admitted work. |
| Other read surfaces | The same audience check covers session inventory, search, history, files, descendants, progress, streams and debug exports. Stream checks repeat while connected. Provider failures do not widen access. |
| Model context | Ishaan’s #559 supplies thread history through the shared `SessionContext` pipeline at execution time, with bot-first reads and an authorized connection fallback. A regression verifies the actual model request receives this reference in its instructions. Authored text and retained file context remain separate at admission; shared follow-ups rebuild from authored messages. The context capability uses the existing durable-operation mechanism for execution replay. |
| Personal memory | Quoted context cannot become requester evidence. Existing native chat-backed notes with invalid quotes are excluded from recall and marked for owner review in Memory settings. Review & edit saves a manual note; Delete remains available. |
| Attachments | Shows retained originals, with generic failure notices. Preview text and internal diagnostics stay out of public message metadata. An edit to Slack attachments requires a follow-up to give the task new files. |
| Web follow-ups | Newly admitted web messages in Slack chats are mirrored with author attribution and durable per-message/revision identities. Slack-originated inputs are not mirrored back. Older web messages are not backfilled. |
| Slack edits/deletions | Queued text edits update the task's authored prefix and preserve context. Started work retains its original input and shows a correction notice. A queued deletion stops that task; a started deletion hides the transcript body but does not undo work. Older or duplicate updates cannot resurrect a deleted message. |
| Delivery | Known rate-limit rejections retry after the provider delay. A lost/ambiguous response stays in confirmation and is reconciled by reading Slack metadata. It is never blindly posted again. Separate pending and confirmation queues prevent an uncertain backlog from starving new sends. |
| Delivery recovery | The chat shows waiting, confirming or rejected delivery. The owner can retry explicitly rejected sends. Unknown sends are excluded from this action. Reconnected or revoked authorities cannot silently restore an old task's permissions. |
| Long chats/navigation | Loads pages after the first 200 messages. A transcript version avoids refetching unchanged later pages. Route changes discard old responses and protected UI state; browser regressions cover slow navigation. |
| Historical continuation | Records the original Slack destination in the same transaction as the first native admission. Historical receipt-backed messages retain attribution. Existing source access checks still apply. |
| Legacy repair | Validates the actor, thread, timestamp and admission receipt before removing an old generated suffix. Ambiguous file-context bodies require review. An encrypted recovery copy and the original execution input remain intact. |

## Upgrade and repair

Run these commands in the release environment with its existing product/native database configuration and encryption keys. A local test database is not the deployed database.

1. Use the [maintenance control contract](pydantic-deployment.md#native-maintenance-control-contract) to pause admission and park work. Check the persisted status and unresolved effects. Retire **all** previous API, coordinator, broker and worker owners before schema provisioning. Managed deployments must use the maintenance release path with the exact qualified source commit; API-only and rolling releases are incompatible with this schema change. Older coordinators cannot decode the new `sealed:v1:` hook payloads.
2. Back up the databases and encrypted object storage through the deployment's existing backup procedure. Retain the matching keys. Do not reset receipts or regenerate keys.
3. Provision and verify using the new release:

   ```sh
   .venv/bin/python -m moyai.product_start provision --apply
   .venv/bin/python -m moyai.product_start verify
   ```

   Provisioning should return the v21 application revision. Verification should return `app_schema="verified"` and `harness_schema="verified"`. Those receipts do not assert runtime readiness or authorize resume.

4. Review the bounded repair report while maintenance remains paused:

   ```sh
   .venv/bin/python -m moyai.message_source_repair > slack-repair-preview.jsonl
   ```

   Each line contains message/session IDs and `repair`, `review` or `unchanged`; it contains no message bodies. `review` identifies an ambiguous old attachment boundary. `unchanged` means there was insufficient evidence to rewrite a row. After reviewing the report, apply it:

   ```sh
   .venv/bin/python -m moyai.message_source_repair --apply > slack-repair-applied.jsonl
   .venv/bin/python -m moyai.message_source_repair > slack-repair-after.jsonl
   ```

   Apply requires a paused product maintenance state and retired native workers. Repeating apply is idempotent. It does not execute tasks or change frozen `Turn.prompt` values. Preserve reports with restricted operator access because they contain conversation identifiers.

5. Start only new-release roles. Verify their registered version/policy and health using the existing deployment runbook. Resume through the maintenance control contract after safe-boundary checks pass.
6. Perform the live checks below. Keep the rollout marked unverified until they pass on the actual deployment.

Do not roll back an old coordinator onto encrypted pending hooks or remove the v21 tables while new roles are active. If a rollback is needed, pause and retire the new roles, preserve the data, and use a reviewed compatible release or restore procedure. This change supplies no automatic downgrade.

## Slack permissions and recovery

The OAuth request retains the scope set used before the message-provenance change. The base bot scopes are `app_mentions:read`, `chat:write`, `im:write`, `im:history`, `reactions:read`, `reactions:write`, `assistant:write`, `files:write` and `files:read`. Identity linking adds `users:read` and `users:read.email`; thread chat adds `channels:history`, `groups:history`, `channels:read` and `groups:read`. User-token scopes remain `search:read`, `channels:history`, `groups:history`, `im:history` and `mpim:history`.

Neither `im:read` nor `mpim:read` is requested. DM access uses the verified requester relationship without a conversation-info call. Group conversations remain unavailable in the browser if the existing installation cannot verify their audience; this cleanup does not expand permissions to enable them.

For **Slack access could not be verified right now**, retry the read shortly. Provider timeouts, rate limits and other unavailable responses return HTTP 503; rate limits preserve `Retry-After`. Operator logs record `Slack audience check` with a safe error code, HTTP status and retry delay, without credentials or provider payloads. These failures do not expose the conversation or cancel an already admitted turn.

For **Conversation unavailable**, check the signed-in user's verified Slack account, profile freshness, workspace, channel membership and the connection in **Settings → Connections**. Inspect the recorded reason before changing the installation. Reconnecting changes installation authority: existing tasks/destinations remain fenced and do not silently regain execution or delivery permission. Use a new Slack conversation for work under a replacement installation. Do not manually rewrite old turn-authority records to bypass this fence.

For **Confirming Slack delivery**, inspect the original Slack thread and allow read reconciliation to settle it. Do not repost an uncertain send. For an explicit rejection, correct the connection/access problem and use **Retry delivery** if the original authority remains valid. A rate-limit rejection retries automatically.

## Retention boundaries

Pending product hook payloads are encrypted and expire after seven days without admission. Completed/rejected payload bodies are cleared. The inbox retains terminal bookkeeping for a bounded period; it is not a permanent audit log.

A transcript tombstone is not an erasure request. Frozen task inputs, diagnostic exports, legacy repair backups and retained attachment objects continue to follow the deployment's storage policy. File inspection caches remain private to the original sender. This change does not introduce a general object-storage garbage collector or shorten existing object retention.

Requester debug exports intentionally include the original received prompt as diagnostic context. They remain requester-scoped and subject to the Slack audience checks. Public transcript/search consumers receive authored text. External inference gateway and logging retention are deployment settings and were not inspected as part of these code changes.

## Verification

Automated coverage uses real encrypted native records, disposable local PostgreSQL, verified identity logic, synthetic Slack membership responses and a local HTTP receiver. Browser tests use the real React app with synthetic API data. These checks do not prove Slack provider configuration or production deployment.

The execution regression admits a Slack input with eight selected GitHub repositories, runs concurrent credential discovery through the real connection toolset, and verifies that a model request occurs with zero Slack audience API calls during provider failure. Separate tests retain installation, sender, connection, policy, stopped-task and deleted-session fences, and check the OAuth scopes under every feature-flag combination.

Run from the repository root:

```sh
npm run typecheck
npm test
npm run build
npm run test:ui
.venv/bin/ruff check native/src native/tests
.venv/bin/ruff check --select F821 app
.venv/bin/pyright --project native/pyproject.toml --pythonpath "$PWD/.venv/bin/python"
MOYAI_TEST_POSTGRES=postgresql://localhost/moyai_test .venv/bin/pytest native/tests
```

Replace the PostgreSQL URL with an authorized disposable **local** test server. Test fixtures create and drop their own databases; never use the production server. The CI suite runs the same native tests in four shards.

Live acceptance checks require the actual Slack installation:

- Send a distinctive Slack message, then open its Moyai chat as that sender. Confirm Slack/name/**You**, only the authored body, correct time and an exact source link. A second sender must show their name without **You**.
- Check a public channel, private channel and DM. Confirm that an unrelated viewer cannot find/read the private chat or retrieve its file URLs. Remove membership and confirm active reads/streams stop exposing it.
- Add an original document and an unsupported preview type. Confirm the original is accessible to the right audience and preview failure is explained.
- Send a web follow-up. Confirm one attributed Slack copy and one answer. Edit queued input, then change a started message and delete another queued message; confirm the documented behavior without an extra task.
- Exercise a known rate limit and a deliberately uncertain send in a controlled test installation. Confirm retry versus confirmation behavior and no duplicate posts.
- Review repaired old messages and flagged personal-memory notes. Confirm the original reported chat after repair. Verify long-history navigation and refresh with real data.

Do not run the send/failure exercises against other people's conversations without their authorization. The previously observed production navigation mismatch has a regression test; its original production cause was not established.
