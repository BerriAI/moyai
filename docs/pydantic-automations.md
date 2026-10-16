# Canonical automations on the Pydantic core

The product `automations`, `automation_runs`, `automation_events`, and receiver
configuration remain canonical. Native tables hold execution bindings, immutable
authority selections, native occurrence links, operation receipts, and grants
for the current human turn. No legacy run or executor is created.

## Assembly

Construct `product_identity`, `product_connections`, `product_workspace`,
`product_continuations`, and `product_secrets` before creating the automation service:

```python
runtime.product_automations = ProductAutomationService.from_runtime(runtime)
runtime.product_automation_events = runtime.product_automations.events
```

Register `product_automation_tools.capability(service)` after EffectReceipts
and before TemporalDurability. Its FunctionToolset ID is
`moyai_product_automations_v1`. Register the exported
`PRODUCT_AUTOMATION_WORKFLOWS` and `PRODUCT_AUTOMATION_ACTIVITIES`, mount
`product_automations.routes(service)`, and call `service.dispatch_pending()` from
the existing coordinator dispatcher. The exported lists include schedule sync,
calendar admission, event admission, and the factual pre-body-refusal activity.
There is no new process timer or model loop.

`Database.admit_automation(..., connections=...)` must preserve the caller's
transaction. `Database.resolve_grants` must pass `db=db` to
`product_connections.resolve_grants`. Source reads during canonical admission
must use that same transaction; checking out another pool connection while
holding the canonical writer lock can deadlock.

The fixed EffectReceipts mutation allowlist must contain:

```python
{
    "automation_create", "automation_update", "automation_migrate",
    "automation_enable", "automation_pause", "automation_retire",
    "automation_grant", "automation_self_update", "automation_claim_item",
    "automation_webhook_setup",
}
```

The direct tools require a current individual requester, a persisted shared
direct chat and the trusted root run identity. Shared-password authentication
does not authorize these tools. The original explicit local-preview exception
is retained. Scoped tools recheck the persisted occurrence or human-turn grant;
they cannot grant themselves general management rights.

Verified mapped Slack direct chats use
`identity.individual_for_admission(db, scope.product_grant)` during explicit
management interactions. The raw Slack scope, original turn authority and effect
identity remain intact. Operation ownership and explicit enable/migrate capture
the canonical individual owner. Scheduled admissions never call that resolver.
Freshness, ambiguity, disabled owners, delegated work and persisted private
visibility are checked before mutation and again after asynchronous work.

Native credential/approval resumptions have no new user Message. Management and
temporary grants follow the persisted resume fingerprint back to the original
server-authored human turn. Request-key prefixes confer no authority; the chain
is bounded and every turn must retain the same actor, session and original grant.

Every mutation first claims its matching pending effect with an attempt nonce.
Canonical mutation, operation receipt and confirmed effect commit atomically.
A trusted refusal after acknowledged rollback settles only that attempt as
`not_dispatched`. A postcommit refusal retains `confirmed`; transport/commit
uncertainty retains `pending`.
The native interceptor's pre-body refusals use a replay-patched public capability
hook and explicit evidence activity; no database operation runs in workflow
context. Register the capability after EffectReceipts so the known refusal is
not converted into a generic confirmed receipt.

Provision the additive schema versions before startup, including execution
context v4 and chat-grant v8. Previous receipts are not rewritten. Event
authority uses a tagged JSON envelope inside the existing immutable context
column; it does not add a column to an already published schema version.

## HTTP contract

The authenticated base path is `/api/product/automations`. Writes use the
canonical product Origin and `X-CSRF-Token` checks.

| Route | Request or result |
| --- | --- |
| `GET /` | `automations`, `next_offset`, `event_choices`, `source`, `migration`, `capabilities`; shared library rows include `owner_id` and `owned` |
| `GET /{id}/preview` | Full canonical `definition`, revision, native generation/sync state, blockers and per-action capabilities |
| `POST /` | `{revision: 0, request_key, definition}`; saves paused |
| `PUT /{id}` | `{revision, request_key, definition}`; saves paused |
| `POST /{id}/migrate` | `{revision, request_key, activate: false}`; explicit opt-in |
| `POST /{id}/enable`, `/pause`, `/retire` | `{revision, request_key}` |
| `POST /{id}/run` | `{revision, request_key}`; 202 with native session/turn IDs; does not enable recurring execution |
| `GET /{id}/history` | `history`, `next_cursor`, `untrusted_reference`; actual native/source status with private/deleted sessions excluded |
| `GET /{id}/webhook` | Receiver readiness, provider URLs/examples and actual delivery metadata; no secrets |
| `POST /{id}/webhook` | `{revision, request_key, provider, secret?}`; configures canonical receiver, increments revision and pauses |
| `POST /{id}/test-event` | `{provider, event_header?, payload}`; returns match/context and `started: false` |

Request keys are stable ASCII `[A-Za-z0-9_-]{8,80}`. Trigger IDs must be explicit
and stable. Mutation results report `saved` and the actual pending/ready state;
pending does not mean a confirmed next execution time.

Webhook setup returns a generated secret only when the owner explicitly leaves
the value blank. An identical retry can recover that same generated value.
Supplied secrets are never echoed. Setup does not register a remote provider
webhook. Linear and PagerDuty require the provider's signing secret.

The receiver URLs remain `/hooks/automations/{id}/{provider}` and
`/hooks/automations/{id}`. Verification reuses
`app.automation_webhook_auth.verify_webhook`; both engines use the same pure
signature, timestamp, body-limit and delivery-deduplication contract.

## Migration and admission

Migration captures the owner's authenticated original grant, increments the
source revision, pauses the source row and sets its harness marker to
`pydantic-v1`. Native activation waits for acknowledgement that old schedule IDs
are paused. The marker also fences the old manual-launch path, which otherwise
can bypass the paused flag.

Calendar behavior reuses `Timing.spec()` for hourly, daily, weekdays, weekly,
cron, once and IANA timezones. Temporal owns overlap and catch-up. Once receipts
use the original product's exact occurrence key. A consumed once schedule is
not reset during reconciliation.

Each occurrence holds the canonical writer lock and verifies source ownership,
revision, digest, generation, intent and the saved original grant. It resolves
captured connection IDs, never provider names or a fresh owner grant. Explicit
owner enable/migrate can authorize new selections. Native session/turn creation
and canonical occurrence receipts share one transaction.

Repository selections are resolved to permanent GitHub IDs during explicit
create/update/migrate/enable, outside the canonical writer transaction. Public
lookup is anonymous; selected GitHub lookup must retain the original exact
connection authority. The writer rechecks source revision, original actor
authority and the selected connection IDs before saving. An identical committed
operation recovers before another provider lookup. Scheduled occurrences never
resolve a new repository identity from a saved URL.

Event routing uses `native_pending` and `native_dispatched` in the canonical
inbox. The old dispatcher selects only `pending`. Acknowledged workflows leave
the pending-dispatch queue so a waiting automation cannot starve unrelated work.
Queue/rate waiting is durable Temporal sleep; source limits and 24-hour expiry
are retained. Unknown outcomes are not blindly re-admitted.

Session capture starts at activation, excludes private, deleted, automated and
delegated sources, and rechecks the private sink at admission. Reference history
is bounded and marks truncation explicitly. Slack delivery comes only from
ProductSlack's verified hook; the hook must establish a non-DM/non-MPIM,
non-external shared channel before forwarding. Event-source authority does not
automatically grant the agent corresponding connection tools.
Session filters accept both original 32-character IDs and exact native UUIDs.
Old IDs use the continuation service's recorded mapping; UUIDs are never stripped
or guessed. This is the shared event DTO's sole schema extension.

Scoped self-updates can change only owner-granted instructions or timing of
existing schedule IDs. They retain enabled/paused intent and the original saved
authority. Item claims consult historical product claims as well as native
claims; failed work does not release a claim for an accidental duplicate PR.

## Qualification boundaries

Native tests use isolated PostgreSQL, the real Temporal SDK/worker, stock
FunctionToolset execution, replay, canonical encrypted connection rows, and
synthetic principals. These checks supplement deployed-application proof with
the configured real provider; they do not replace it.

The workspace adapter supports validated environment images and bare repository
checkout through `ProductConnections.prepare_repository`. Missing preparation
support is an explicit blocker. Public checkout does not borrow credentials;
private checkout and private environments require the selected current GitHub
repository grant. The prepared-image path passed an actual Temporal/model/Modal
automation with an exported marker and exact Git HEAD from a real validated
snapshot. Its canonical environment inputs were seeded after the real image
verification; the environment creation/build UI was not part of that proof.
`mode=demo` is explicitly unsupported by this Modal execution adapter.

`automation_webhook_setup` accepts only an opaque native
`credential_request_id`. The current request must resolve to a non-expiring,
persistent **organization** credential named `webhook-signing-secret`, in env
format with `WEBHOOK_SECRET`. A personal credential cannot be copied into the
shared receiver. The original raw execution, request handle, current credential
revision, ownership and persisted surface are rechecked in the same transaction.
The canonical receiver write and effect confirmation commit together. Neither
the tool result nor the operation/effect receipt contains the secret.

The tool is exposed when `product_secrets.execution` is configured. The assembled
agent also needs `product_credential_tools.capability(execution)` and the native
request-resolution UI/API to obtain a handle. A real PostgreSQL and Temporal
regression exercises request, defer, resolve, native resume, receiver
configuration and replay without a second mutation. This is supplementary to a
deployed agent credential proof. The actual CUA form subsequently passed
organization/persistent selection, native resume, real-model receiver setup,
confirmed receipt, encrypted storage and no secret in chat/activity. The
receiver remained paused; invalid signatures were refused and repeated valid
delivery was deduplicated.

An isolated actual-app rehearsal used baseline
`2f6823623d8eaec6eb9afdb7009fa3c13f377ee5` and a source snapshot of the candidate.
The baseline created an active canonical/Temporal schedule. The baseline process
was stopped before native migration. Native activation acknowledged the old
schedule's pause, retained the same definition/owner, and created no legacy
execution rows. A real Temporal once trigger and signed local webhook each
completed on the configured real gateway/model and Modal; duplicate delivery
created no second occurrence. Downloaded artifacts contained the independently
checked results `15` and `5040`. Both shell receipts were confirmed, and all six
inference responses had explicitly recorded actual costs. This proof covers
base-tool automations; it does not claim a live remote-provider webhook, mapped
Slack delivery, or remote provider write was exercised. A subsequent native
session-message trigger also completed with an exported `81`; retrying the
source message created no additional occurrence.
An actual public-repository automation also resolved and saved the permanent
repository ID, ran from a Temporal once schedule without a selected GitHub
credential, and exported the independently checked README and Git HEAD.

After refreshing the isolated backend snapshot with the final authorization
changes, a real model turn for an explicitly synthetic, locally enrolled
individual listed, created and previewed a paused automation. The canonical
owner was preserved and `automation_create` had a confirmed receipt. The same
request under shared-password authentication was refused without creating a
definition or leaving a pending effect. This checks the actual product identity
adapter and tool policy; it is not evidence of a real Google SSO login.

The tests use isolated local resources. Passing them does not by itself qualify
a production cutover.

An actual credential approval after a roughly thirty-minute wait exposed a
separate core recovery gap: the Modal computer had expired, and the resumed
turn failed before receiver setup. The workflow currently skips computer
snapshot settlement for deferred results and does not restore `Session.snapshot`
when the old reference is unavailable. The normal credential proof used a fresh
task within the existing idle lifetime; it does **not** qualify long human waits.
The workflow/runtime owner must close that checkpoint/recovery gap before full
durable approval readiness is claimed. No idle timeout was increased.
