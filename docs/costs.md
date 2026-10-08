# Model and infrastructure costs

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Spend dashboard

**Settings → Administration → Spend & usage** (`#spend`) gives administrators four tabs:

- **Overall:** recorded infrastructure and model costs, active sessions, and model requests over time.
- **Users:** team-wide human requests and active teammates, followed by a sortable spend table with costs, sessions, model requests, tokens, and share of LLM spend. Filter a user to inspect their costs and sessions; activity remains team-wide.
- **Usage history:** daily model costs, a cumulative cost line, a model filter, and exact request details.
- **Infrastructure:** existing provider sync, monthly bills and estimates, coverage, and Slack identity controls.

Use the date menu for presets or a custom UTC range of up to 93 days. **Export CSV** exports the current report and user/model filter. The Users export includes the selected spend rows and a separate, labeled team activity section. If activity cannot load, spend stays available with an **Export spend CSV** action. Charts and daily exports use the full scoped ledger; the request-detail disclosure remains limited to the latest 500 requests. An active session can appear on multiple days, while the period total counts it once. Unknown costs stay visibly unpriced, and averages use recorded costs only.

Members retain **Settings → Workspace → Spend** for their own model spend and established linked Slack activity. Organization charts and infrastructure controls require an administrator report from the server. The **Users** tab brings human activity and model spend together while distinguishing human submissions from model requests: one human submission can produce many model requests. Existing `#adoption` links open the combined Users tab.

For a local visual demo with synthetic data, run `node scripts/settings_ui_preview.cjs --port 8953` and open `http://127.0.0.1:8953/#spend`. This preview does not contact providers or exercise production authentication and billing writes.

## Per-user model spend

**Live verification (September 29, 2026):** deployed commit `87aa874` on Render. Session `9895bd3b52a9403a988f4dc06c7545dd`, started by `tin@berri.ai`, completed an Astra response, then an Opus follow-up that recalled `granite-27`, wrote a local file and read it back. Three successful inference responses supplied `x-litellm-response-cost`: `$0.061130000000000004`, `$0.037141`, and `$0.0042726`, totaling `$0.102543600000000004` (displayed as `$0.102544`). User, session and model totals agree. Both successful turns saved filesystem/conversation snapshots and terminated their Modal sandboxes. The admin UI exposed the exact header amounts; unauthenticated spend access returned 401 and the removed callback route returned 404.

Initial verification exposed LiteLLM's rejection of request-level `turn_off_message_logging`. Moyai no longer sends that option; gateway security settings were not changed. Seven rejected attempts remain visible as unknown cost, rather than assigning an invented zero. A separate minimal diagnostic request cost `$0.00029` on the same key, outside the Moyai session ledger. The gateway still has no key-scoped logging integration, and `allowed_routes` remains `llm_api_routes`. The full 110-test suite passed before deployment; all 22 spend/model tests passed after the logging-policy correction.

Administrators can open **Settings → Administration → Spend & usage** to see USD model costs by user, session and model, with UTC date filters (up to 93 days). Google sign-in registers a stable account using the verified Google subject, so an email/name change does not create a different billing identity. New sessions record their creator; every queued message independently records its sender. A response, including its tool loops, retries and context compression calls, is attributed to that sender even when another teammate queues the next message. Conversations retain shared-workspace visibility; this adds ownership and accounting, not private chats.

Each model call is durably reserved before reaching LiteLLM, with the active message sender, session, selected model and existing key fingerprint. Sandbox-supplied attribution is ignored. All requests use the same existing gateway virtual key; SSO user/session accounting stays inside Moyai. Normal response capture needs no new gateway users, keys or callbacks. Background receipt recovery additionally needs the read permission described below.

Moyai saves the finalized `x-litellm-response-cost` from non-streamed inference responses, falling back to `usage.cost` when present. Legacy `x_litellm_response_cost` fields in usage or at the response top level remain supported; `usage.cost` takes precedence among body fields. These are gateway-reported costs, not token-price estimates. Token counts are retained for context; cache pricing and other gateway pricing rules are already reflected in its returned cost. Decimal costs are parsed, stored as strings and summed without binary floating-point rounding.

Because streamed HTTP headers arrive before generation finishes, they are ignored for pricing. Native Messages and Responses streams pass through as received; Moyai captures cost from final `message_delta.usage` or `response.completed.response.usage`. The gateway must emit `usage.cost`: LiteLLM's `litellm_settings.include_cost_in_streaming_usage: true` enables Messages cost injection; verify native endpoint support in the deployed gateway version. Capturing these fields adds no network hop or response buffering. Unknown costs remain visible, separate from explicit zero-cost responses.

For Hermes Chat Completions, the broker requests a non-streamed completion from LiteLLM. It saves the final cost and usage before returning that response, adapting completed text/reasoning/tool calls to the Chat Completions SSE protocol when Hermes requests streaming. Tool and session activity still update live; individual inference tokens are delivered together after completion. If a sandbox stops while an already-submitted inference completes, its returned cost is still recorded.

User totals add up to returned costs and recovered receipts for tracked Moyai requests; the dashboard shows coverage and expandable exact per-request amounts. This does not audit the key's entire lifetime spend: earlier usage, calls outside Moyai, gateway-internal billed attempts not reflected in the returned cost, and interrupted requests without a final response cannot be recovered from response headers. Tracking starts at rollout. Gateway key rotation preserves historical charges in the organization report. Demo sessions incur no model spend. Infrastructure costs appear alongside LLM costs as described below.

### Recovering costs after an interruption

Moyai saves each new attempt's request ID, key fingerprint and gateway scope before submitting inference. A background worker retrieves a receipt for a finalized request whose cost is still unknown. Execution remains `interrupted` or `failed` even when billing settles; the spend page shows **Cost pending**, **Recovered receipt**, or an unresolved cost. Recovery never reruns inference, estimates token prices, changes original attribution, or replaces an existing cost.

Keep `include_cost_in_streaming_usage: true` on the gateway for normal streaming capture. For recovery, allow the existing Moyai virtual key to call **`/spend/logs/v2`** in addition to its existing inference routes. Preserve every existing key permission when adding this route. No master/admin key is needed. The gateway must support exact `request_id` lookup by `litellm_call_id` and return complete, uncapped per-request records (verified with LiteLLM 1.104.1). A 401/403 leaves the cost unresolved and the UI reports **Receipt access required**; inference continues normally. `LITELLM_SPEND_RECOVERY_ENABLED=false` disables the worker without changing response capture.

Lookups begin after a 60-second grace period, use one HTTP connection, and process at most 16 rows per polling cycle. Failures and delayed receipts retry with exponential backoff capped at one hour. The indexed pending queue survives restarts in the existing SQLite database. A conditional update and a unique gateway/key/receipt constraint prevent duplicate settlement. Recovery runs its database operations in a worker thread and joins an outstanding write during shutdown. No receipt lookup or additional proxy hop is added to inference. SQLite contention and ordinary request writes remain; in Modal deployments, the existing shared checkpoint lock can also delay inference while a background snapshot commits. Load measurements should identify their deployment topology.

Only an unambiguous positive receipt under the captured key and gateway can repair a missing cost. Multiple provider receipts, conflicting IDs, incomplete results and default-zero spend records remain unresolved; explicit zero costs in normal responses are still valid. No receipt can be recovered after the gateway deletes it. Pre-migration requests lack a captured gateway scope and are left unchanged for a separate reviewed backfill. Changing the gateway or rotating the key preserves recorded amounts, but unresolved requests from the old scope require separate recovery. Title generation, transcription, external calls and gateway-internal retries outside the tracked ledger are not included in this recovery guarantee.

Slack messages capture the sender from verified Slack events. With `SLACK_IDENTITY_LINKING_ENABLED=true` (default), Moyai looks up a sender’s Slack profile in the background on first use, creates a local profile labelled with their company email, and automatically links it to a unique Google identity with that exact email. Slack-first and Google-first usage both work: a first Google login combines earlier Slack spend automatically. Original message, session-owner, and inference sender IDs remain immutable; automatic and manual link changes are audited.

Reconnect the dedicated Slack app with **bot scopes `users:read` and `users:read.email`** to enable lookup. Existing senders with recorded sessions/messages are backfilled automatically; **Spend → Infrastructure → Slack identities → Refresh profiles** queues a recheck. Profiles are refreshed daily, with failures retried after five minutes. Missing permission or lookup failure never blocks a chat. Only members of the installed Slack workspace with an email in `GOOGLE_ALLOWED_DOMAINS` qualify; guests, external Slack Connect users, bots and deleted users do not. Emails are trimmed and lowercased, without alias, dot or plus-address normalization. Email changes and ambiguous matches require administrator review; automatic matching never retargets an existing link, and explicit admin overrides survive refreshes. The secondary **Administrator overrides** controls remain available for exceptions.

This is **just-in-time identity provisioning and accounting linkage**, not SCIM: it does not create a Google Workspace account, grant a web session or administrator role, or synchronize directory groups and deprovisioning. Web access still requires verified Google OIDC login. Before that first Google login, the Slack-created profile and its spend appear under the company email.

**Activated September 30, 2026:** the approved profile/email scopes are installed on BerriAI’s dedicated Moyai app and the encrypted organization connection was refreshed. Live backfill resolved all four existing session senders: one retained its admin-selected Google link; three received company-email profiles awaiting their first Google login. The recorded total remained unchanged. The migration preserved all 26 sessions and has a private pre-migration SQLite backup on Render. Validation: 186 Python tests and three chat-stream tests passed; automatic matching in both sign-in orders was covered in tests, while live verification covered real Slack profile lookup and preservation of the existing link.

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

### Pull requests and leaderboard

Administrators can open **Spend & usage → Pull requests** to inspect confirmed Moyai PR publication receipts, their requesting contributor, current GitHub status and linked sessions. Search and status filters also apply to CSV export. This table selects PRs first tracked during the chosen UTC dates.

**Leaderboard** ranks contributors by unique PRs merged during the selected dates, including PRs created earlier. **PRs created** uses verified GitHub creation dates; **PRs by status** shows the current status of that creation cohort. A PR created in the period may merge later, so those status counts can differ from the period's merged count. Contributors with creations and no merges remain visible. Select a contributor to inspect both cohorts. Unknown creation dates are reported separately and never inferred from the date Moyai first tracked the PR.

Credit uses the original publication message sender, with established Slack/Google accounting links; shared or missing identities remain unattributed. Duplicate receipts count once by permanent repository ID and PR number. A missing verified merge date never increases the merged count.

**Linked session LLM spend** covers all recorded usage in each linked root session and its direct children, including follow-up work. It is a correlation, not an exact cost allocation to a PR. One session may produce multiple PRs, so per-PR amounts overlap. Contributor rows count each linked session once within that row; rows can still overlap when contributors share a session. Pending and missing prices remain marked. Infrastructure costs are excluded.

**$ per merged PR** divides that contributor's deduplicated linked-session spend behind PRs merged in the selected dates by their merged PR count. The spend is all-time recorded LLM usage, including follow-up work. No merges yields an unavailable ratio rather than zero; pending or missing prices make the ratio partial. Displayed currency rounds to cents, while CSV preserves the decimal report values.

The admin-only `/api/admin/pull-requests` endpoint uses the same `start`/`end` date contract as Spend. Verified GitHub snapshots and merge dates persist in SQLite and the normal database checkpoint. A restart restores them as stale until refreshed. Status reads reuse the current GitHub connection's selected repositories, bounded refresh queue and credential-rotation checks. A disabled connection leaves retained publication history visible with unknown remote status; an inaccessible PR is never inferred to be closed. Deleted sessions retain their financial history but have no navigation link in the report. PRs published outside Moyai's confirmed publication tools are not inferred from chat URLs.
