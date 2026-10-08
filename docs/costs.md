# Model and infrastructure costs

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Per-user model spend

**Live verification (September 29, 2026):** deployed commit `87aa874` on Render. Session `9895bd3b52a9403a988f4dc06c7545dd`, started by `tin@berri.ai`, completed an Astra response, then an Opus follow-up that recalled `granite-27`, wrote a local file and read it back. Three successful inference responses supplied `x-litellm-response-cost`: `$0.061130000000000004`, `$0.037141`, and `$0.0042726`, totaling `$0.102543600000000004` (displayed as `$0.102544`). User, session and model totals agree. Both successful turns saved filesystem/conversation snapshots and terminated their Modal sandboxes. The admin UI exposed the exact header amounts; unauthenticated spend access returned 401 and the removed callback route returned 404.

Initial verification exposed LiteLLM's rejection of request-level `turn_off_message_logging`. Moyai no longer sends that option; gateway security settings were not changed. Seven rejected attempts remain visible as unknown cost, rather than assigning an invented zero. A separate minimal diagnostic request cost `$0.00029` on the same key, outside the Moyai session ledger. The gateway still has no key-scoped logging integration, and `allowed_routes` remains `llm_api_routes`. The full 110-test suite passed before deployment; all 22 spend/model tests passed after the logging-policy correction.

Administrators can open **Settings → Spend** to see USD model costs by user, session and model, with UTC date filters (up to 93 days). Google sign-in registers a stable account using the verified Google subject, so an email/name change does not create a different billing identity. New sessions record their creator; every queued message independently records its sender. A response, including its tool loops, retries and context compression calls, is attributed to that sender even when another teammate queues the next message. Conversations retain shared-workspace visibility; this adds ownership and accounting, not private chats.

Each model call is durably reserved before reaching LiteLLM, with the active message sender, session, selected model and existing key fingerprint. Sandbox-supplied attribution is ignored. All requests use the same existing gateway virtual key; SSO user/session accounting stays inside Moyai. No new gateway users, keys, callbacks or reporting permissions are required.

Moyai saves the finalized `x-litellm-response-cost` from non-streamed inference responses, falling back to `usage.cost` when present. Legacy `x_litellm_response_cost` fields in usage or at the response top level remain supported; `usage.cost` takes precedence among body fields. These are gateway-reported costs, not token-price estimates. Token counts are retained for context; cache pricing and other gateway pricing rules are already reflected in its returned cost. Decimal costs are parsed, stored as strings and summed without binary floating-point rounding.

Because streamed HTTP headers arrive before generation finishes, they are ignored for pricing. Native Messages and Responses streams pass through as received; Moyai captures cost from final `message_delta.usage` or `response.completed.response.usage`. The gateway must emit `usage.cost`: LiteLLM's `litellm_settings.include_cost_in_streaming_usage: true` enables Messages cost injection; verify native endpoint support in the deployed gateway version. Capturing these fields adds no network hop or response buffering. Unknown costs remain visible, separate from explicit zero-cost responses.

For Hermes Chat Completions, the broker requests a non-streamed completion from LiteLLM. It saves the final cost and usage before returning that response, adapting completed text/reasoning/tool calls to the Chat Completions SSE protocol when Hermes requests streaming. Tool and session activity still update live; individual inference tokens are delivered together after completion. If a sandbox stops while an already-submitted inference completes, its returned cost is still recorded.

User totals add up to costs returned for tracked Moyai requests; the dashboard shows coverage and expandable exact per-request amounts. This does not audit the key's entire lifetime spend: earlier usage, calls outside Moyai, gateway-internal billed attempts not reflected in the returned cost, and interrupted requests without a final response cannot be recovered from response headers. Tracking starts at rollout. Gateway key rotation preserves historical charges in the organization report. Demo sessions incur no model spend. Infrastructure costs appear alongside LLM costs as described below.

Slack messages capture the sender from verified Slack events. With `SLACK_IDENTITY_LINKING_ENABLED=true` (default), Moyai looks up a sender’s Slack profile in the background on first use, creates a local profile labelled with their company email, and automatically links it to a unique Google identity with that exact email. Slack-first and Google-first usage both work: a first Google login combines earlier Slack spend automatically. Original message, session-owner, and inference sender IDs remain immutable; automatic and manual link changes are audited.

Reconnect the dedicated Slack app with **bot scopes `users:read` and `users:read.email`** to enable lookup. Existing senders with recorded sessions/messages are backfilled automatically; **Spend → Slack identities → Refresh profiles** queues a recheck. Profiles are refreshed daily, with failures retried after five minutes. Missing permission or lookup failure never blocks a chat. Only members of the installed Slack workspace with an email in `GOOGLE_ALLOWED_DOMAINS` qualify; guests, external Slack Connect users, bots and deleted users do not. Emails are trimmed and lowercased, without alias, dot or plus-address normalization. Email changes and ambiguous matches require administrator review; automatic matching never retargets an existing link, and explicit admin overrides survive refreshes. The secondary **Administrator overrides** controls remain available for exceptions.

This is **just-in-time identity provisioning and accounting linkage**, not SCIM: it does not create a Google Workspace account, grant a web session or administrator role, or synchronize directory groups and deprovisioning. Web access still requires verified Google OIDC login. Before that first Google login, the Slack-created profile and its spend appear under the company email.

**Activated September 30, 2026:** the approved profile/email scopes are installed on BerriAI’s dedicated Moyai app and the encrypted organization connection was refreshed. Live backfill resolved all four existing session senders: one retained its admin-selected Google link; three received company-email profiles awaiting their first Google login. The recorded total remained unchanged. The migration preserved all 26 sessions and has a private pre-migration SQLite backup on Render. Validation: 186 Python tests and three chat-stream tests passed; automatic matching in both sign-in orders was covered in tests, while live verification covered real Slack profile lookup and preservation of the existing link.

## One-time historical cost repair

`scripts/spend_backfill.py` is an offline maintenance command. It is never invoked by the server, startup or a scheduler. Use it only after deploying the streaming parser fix and obtaining a complete, ungrouped LiteLLM spend export for the historical interval. It reads the existing `workspace.db` directly without running app migrations or contacting a gateway.

Supply either JSON arrays of per-request rows or **all** uncapped `/spend/logs/v2` page envelopes from the same export, repeating `--logs` for each file. Keep the gateway data unchanged while collecting pages; inconsistent, missing or capped pages are rejected. For raw arrays, the operator must ensure completeness. Do not use aggregate `/spend/logs` output or copy only a visible page. Retain `request_id`, hashed `api_key`, `litellm_call_id`, `metadata`, `spend` and `status`; prompts and responses are unnecessary. Gateway log retention and access determine how much history can be recovered.

Matching requires the receipt's historical key hash and exact call IDs or Moyai metadata IDs. Provider response IDs alone usually differ from Moyai IDs. Conflicting identifiers, multiple billed attempts, unknown receipt status, invalid amounts and zero defaults remain unpriced for review. LiteLLM can default unknown spend to zero, so zero-only records cannot establish recovered coverage. Positive spend on a failed gateway request can still be billed. Existing costs, including zero, and all request statuses, tokens and attribution are preserved.

First run a read-only preview with an explicit timezone-aware cutoff (requests created at or after it are excluded):

```sh
uv run python scripts/spend_backfill.py \
  --db /path/to/workspace.db --logs /private/spend-export.json \
  --before 2026-10-07T23:00:00Z > /private/backfill-preview.json
```

Review `changes`, `recovered_spend`, `skip_counts` and `skipped`. After explicit operator authorization, use the preview's `plan_sha256` with a new backup path:

```sh
uv run python scripts/spend_backfill.py \
  --db /path/to/workspace.db --logs /private/spend-export.json \
  --before 2026-10-07T23:00:00Z \
  --apply --expect-plan REVIEWED_PLAN_SHA256 \
  --backup /private/workspace-before-backfill.db > /private/backfill-applied.json
```

The example cutoff is illustrative; choose the actual historical boundary. Apply rereads the database under a SQLite write lock and refuses a changed plan. It makes a consistent, exclusive backup with mode `0600`, then commits all updates together. Run during a quiet maintenance window because backup and repair hold the write lock. Protect the backup as a full copy of the private database. A database write failure rolls back the repair; an already-created backup is retained. A repeat preview excludes repaired rows. Keep both JSON reports and the backup for the repair record.

Repaired rows use `cost_source=gateway_backfill` (shown as **Earlier record** by the current dashboard). Reports omit key hashes and raw metadata. Decimal parsing preserves the digits in the export; it cannot recover precision already lost in the gateway's floating-point storage. No production repair runs automatically when this script is deployed.


## Infrastructure and total cost tracking

**Settings → Spend** shows infrastructure, LLM, and combined USD costs for the
same UTC date range. Per-user/session/model tables continue to show LLM costs;
shared infrastructure is not arbitrarily charged to individual teammates.
The total is a **recorded subtotal** when a provider has missing days or an LLM
request has no final cost. Coverage does not certify that every company expense
has been connected. Calls through separate credential-proxy keys remain outside
the inference ledger.

- **Modal:** enable `MODAL_BILLING_ENABLED=true` with a Team/Enterprise plan and
  billing-authorized `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`. The reader resolves
  `MODAL_APP_NAME` to its exact app ID without creating resources. Add dedicated
  storage/resource IDs to `MODAL_BILLING_OBJECT_IDS` (comma-separated). Never add
  shared objects unless their full cost belongs to Moyai. Daily reports include
  compute across its app, including agents and environment builds, before
  workspace credits, discounts and other invoice adjustments. The current day
  is excluded. Account subscriptions and unallocated storage need a bill.
- **Temporal Cloud:** set a separate `TEMPORAL_BILLING_API_KEY` with Cloud Ops
  billing-report permission. Daily CSV reports are filtered by exact
  `TEMPORAL_NAMESPACE` (including the account suffix). Contracted costs are
  converted from Temporal's documented USD cents to USD. Account-level fees
  without this namespace are excluded. The newest two UTC dates are excluded
  because exports lag at least 24 hours. The API supports daily reports for the
  current and previous two months; older ranges need monthly bills.
- **Render:** its public API does not expose billing. Add the monthly invoice or
  a labeled estimate, including Moyai's compute, disk, bandwidth and allocated
  plan charges. There is no hard-coded price or inferred zero cost.
- **Other services:** add any provider by name, such as Raindrop, a database or
  storage provider. Enter only the portion attributable to Moyai.

Once connected, the worker refreshes this month and the previous month every six
hours. **Sync providers** queues the selected dates. Temporal report IDs and
idempotency keys are saved before polling, so generation resumes after restart.
Imports atomically replace daily values, including explicit zero usage; failures
retain previously imported costs and show an error. One report per provider is
processed at a time. Provider credentials, signed report URLs and raw reports
are never returned to the browser or logged. Billing settings are server-only.
Only administrators can view costs, sync providers, or change bills; writes
require the existing CSRF protections.

A monthly bill or estimate **replaces** synced usage for that provider/month;
it is not added on top. Use the invoice's net USD amount (negative credit totals
are supported), record its scope in the note, and edit the same bill when an
estimate becomes final. Removing the bill restores synced usage. Partial-month
filters allocate a monthly bill evenly by calendar day; this is an allocation,
not measured daily spend. Bills support stale-edit checks and keep an audit trail.
All ledgers and pending sync jobs live in the existing persistent SQLite database
and follow its checkpoint/backup lifecycle. Changing configured app/resource or
namespace scope hides imports from the old scope rather than mixing projects.

Provider contracts reviewed for this implementation:
[Modal billing](https://modal.com/docs/guide/billing),
[Modal Workspace API](https://modal.com/docs/sdk/py/latest/Workspace),
[Temporal billing](https://docs.temporal.io/cloud/billing-api),
[Temporal HTTP API](https://saas-api.tmprl.cloud/docs/httpapi.html),
and [Render API](https://render.com/docs/api).

For a local UI demo with labeled sample data and no provider credentials:

```sh
uv sync --frozen
uv run python scripts/cost_demo.py
```

Open `http://127.0.0.1:8791/#spend`, select September 1–30, 2026, and add a
monthly bill for another provider. The initial sample values are $44.75 of
infrastructure + $18.75 of LLM costs = $63.50. Demo data lives separately in
`.data/cost-demo`. This is a localhost preview, not a deployed billing connection.
