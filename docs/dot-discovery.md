# General proactive attention

`kind="attention", attention_mode="discovery"` uses one standing, owner-approved
schedule for up to two private read-only accounts. The owner can opt in through
ordinary Dot chat; they do not configure a reminder for each finding. A private
prompt supplies priorities. Optional `attention_sources` provide owner-approved
GitHub repository URLs when the scope is narrower than an account. This option
requires the official GitHub read-only MCP endpoint and repository-bound read
tools. The host checks every read's owner/repository arguments and specific
method: repository lists, PR `get`, and issue/PR `get_comments` are supported.
Issue `get` and hierarchy methods are blocked because GitHub can enrich them
with cross-repository relationships. Records without a verified repository,
out-of-scope records and earlier item references are excluded. Every selected account needs
an approved repository in restricted mode; other providers support account-wide
discovery only in this slice.
GitHub rows need a provider URL to establish repository identity. Number-only
issue rows are excluded in restricted scans: GitHub can follow repository
renames, so the requested name alone does not verify the response. Structured
comment envelopes are decoded, and provider URL casing is normalized.
The existing automation tools own creation, review, enable, pause and timing.
Discovery does not introduce another scheduler or create automations per item.

## Execution and privacy

Execution has two SDK agents inside one Temporal turn:

1. Research receives accounts, approved source references, current time and
   references to unfinished provider items. It never receives private priorities,
   chat, memory or item summaries. Read tools and structured results are bounded.
2. The host validates account/source/version/quote evidence and seals a snapshot
   with the observed item revisions. Private judgment receives this snapshot,
   bounded recent Dot context and existing read-only canonical memory. It has no
   connector, computer, scheduling, delegation or memory-writing tools.
3. Settlement rechecks the pinned authority, updates items and publishes at most
   one message atomically through the existing delivery receipt. A quiet check
   may update items but leaves the public chat and SDK checkpoint unchanged.

The canonical admission fence prevents scheduled and manual discovery checks from
overlapping across both phases. Ordinary Dot replies and focused checks remain
independent. Temporal activity replay retains accepted research across worker
restarts; an explicitly failed check is visible as failed rather than quietly
retried with a fresh model budget. Input and tool limits fail visibly. A partial
or blocked scan is recorded in run history, never described as a clean quiet scan.
Provider pagination, next-page cursors and incomplete-result signals also make
a scan partial, as do failed, denied, interrupted or unrecognized tool results.
Unsupported rows inside a nonempty collection also make it partial; silently
dropped rows cannot turn a malformed response into a successful empty scan.
Known paginated tools also remain partial without explicit
first-page exhaustion evidence; bare arrays never imply complete coverage.
A snapshot older than fifteen minutes cannot publish.
The snapshot uses the existing encrypted Temporal payload/offload path and a
sealed native record. It is private execution data, not a public artifact.

## Persistent items

Items belong to the stable owner and selected account plus canonical provider
identity; provider versions are separate. The encrypted ledger records open,
resolved, dismissed and snoozed items. Owner edits win against stale judgments
and remain effective across source updates, schedule edits and replacement chats.
The **Following** dialog in Dot chat supports filtering, resolve, dismiss,
one-day snooze and reopen without replacing the chat draft. Silence or absence
from a bounded scan is not evidence of resolution.
Automatic resolution requires the host to verify a closed/merged GitHub issue or
pull request in the same provider record as the cited identity, version and
quotation. All versions seen in the scan must agree on closure, and the citation
must use the latest verifiable version; conflicting state or versions that
cannot be ordered remain owner-controlled. Partial or truncated scans never
auto-resolve items because discarded records could contradict a closed state.
Other resolution remains owner-controlled. Prose describing an
attempted fix cannot close an item.

Connection and policy authority is rechecked on research, judgment, delivery and
owner item access. Replacing an opaque credential or endpoint conservatively
starts a new account identity; prior private summaries and suppression choices
are not transferred without verified provider identity. Policy/name edits alone
retain item identity. Reopening or snooze expiry permits one fresh notification
of the same source version; merely editing a schedule or replacing the Dot chat
does not.

## Bounds and release

Bounds: four accounts; one trigger; 2,000 characters of private priorities;
12 research tool calls and sixteen model requests; 30 returned records and 16,000
characters per tool result; 12 validated observations; four judgment requests
and four memory reads/searches; 180 KB of serialized model input per request.
The judgment context includes the 30 most recent items and 15 chat messages
(1,500 characters each); every observed item's current revision is fenced even
when its summary is outside that context. The UI lists the 100 most recent items.
These are bounded attention checks, not exhaustive inbox scans.

## Reused infrastructure

| Responsibility | Implementation |
| --- | --- |
| Scheduling, wakeups, replay and worker recovery | Existing canonical automations and Temporal workflow/activity execution |
| Model/tool loop, structured output and bounded correction | Pydantic AI agents, `NativeOutput`, validators and `UsageLimits` |
| Model-visible method restrictions | SDK `AbstractCapability.prepare_tools`; host checks still enforce dispatch |
| Persistent personal memory | Existing Harness memory capability, read-only during judgment |
| Item consistency and notification delivery | Existing database transactions, owner revision checks and delivery receipts |
| Account authority and private execution data | Existing grant checks, encrypted payload conversion and offload |
| Browser/API contract | Pydantic models → OpenAPI → generated hey-api client |

The new domain code decides what a Dot may observe, retain and publish. Provider
normalization only accepts verifiable records; it is not a new connector framework.
There is no new agent dispatch loop, scheduler, retry engine or memory store.
The extra research request allowance covers sequential tools and final typed
output; the SDK still enforces tool and token ceilings.

Existing focused attention definitions retain their old digests and workflow
path. Discovery requires native schema v31 and execution protocol 12; drain old
pollers and apply the registered additive expansion before enabling it.

This slice delivers in the Dot chat. External writes, delegated action-taking,
out-of-app notifications, native Gmail/Calendar adapters, and automatic
natural-language item-control tools are separate work. A GitHub MCP proof does
not establish calendar or email behavior.
