# Intelligence implementation ledger

Status: in progress; validated increments are pushed directly to `main`.
Nothing in this document is a deployment or model-quality claim.

The approved proposal is the scope. This ledger records implementation and
validation as work lands; unmarked items remain open.

## Work packages

- [ ] WP1: Durable input and conversation journal (implementation in progress).
- [ ] WP2: Typed replay, context retrieval, native continuity, runtime qualification.
- [ ] WP3: Slack observation, live context, participation, neutral steering.
- [ ] WP4: Durable goals, budgets, review, wrap-up, and delivery.
- [ ] WP5: Scoped memory, complete skill discovery, repository instructions.
- [ ] WP6: Complete connector/browser/tool/artifact outcomes.
- [ ] WP7: Asynchronous delegation, environments, automation ownership.
- [ ] WP8: Recovery and lazy computer acquisition.
- [ ] WP9: UI, diagnostics, outcome evaluation, migration and rollout docs.

## Implemented foundation (not deployed)

The public journal is stored with the existing database/object store. Stable item
and upload identities make lost acknowledgments retryable. Legacy imports require
a source manifest and complete record count. A renewed computer hydrates from the
server and retains valid local summaries when its journal epoch matches. Incomplete
imports cannot be finalized from a missing or different checkpoint.

Codex now uses the pinned SDK's `thread/inject_items` API, matching QM's typed
replay approach. It preserves user/assistant roles, message phases and real tool
receipts. Unknown outcomes remain explicit reference records. Other SDKs retain
native continuity where already supported; their cold fallback is complete typed
JSON in a text reference. That adapter limitation remains open in WP2.

The public context cache keeps full records and derives compaction allocations from
the selected model input window. `history_read` retrieves all original records by
cursor or an individual record by character range. Session reads no longer clip
message/event text and now paginate children. Redaction of private payloads remains.

The five-pending-message, 100-turn and 2,000-event retention caps are removed.
Composer controls and APIs accept longer messages; the shared contenteditable
editor recognizes an absent maximum. Steering text is neutral rather than labeling
every input a correction.

This requires schema revision 8 and API, worker, broker and coordinator protocol
revision 2. Revisions 1–7 retain main’s published schema history; the journal,
goals, Slack sources, library changes and artifacts ship together in revision 8. Follow the coordinated
migration procedure; the change is not an API-only mixed-version deployment.

Validation so far:

- Real pinned Codex SDK with synthetic provider responses: all seven focused
  transport/replay/recovery cases passed; actual shell, patch and MCP tools execute
  locally. This is protocol validation, not a live intelligence benchmark.
- Journal contract tests cover computer loss, partial imports, stale ownership,
  duplicate identities, multi-megabyte Unicode uploads, lost commit acknowledgments,
  paging/ranges and retained activity beyond old caps.
- The UI bundle builds; 511 Node tests passed before the editor follow-up fix.
- Synthetic browser previews accept 34,000-character messages in new/reply
  composers at 1440, 768 and 320 pixels with no horizontal overflow. Browser
  validation exposed and fixed the editor's absent-maximum handling.
- Foundation regression suite: 588 passed, 15 skipped. Journal contracts: 13 passed.
- Isolated local PostgreSQL 16 migration and journal suite: 33 passed (migration
  tests exercised PostgreSQL; journal fixtures used SQLite). No production
  migration was run.
- Six permanent long-message browser cases passed. Full repository checks and
  other work-package acceptance scenarios remain open.

## Durable goals (implementation in progress; not deployed)

The goal record and cumulative clock receipts now live in the database. The model
registers natural-language requests through one shared tool with explicit outcome,
timebox, floor, cap and deadline semantics. Source input IDs bind creation, revision,
pause and resume. Resume does not reset elapsed work. The old one-hour,
25-continuation and no-tool/repeated-response stops are removed.

Fresh tool-free completion and checkpoint review uses existing gateway accounting,
model routing and context admission. A reviewer can retrieve original journal
records; a new input or receipt invalidates a stale review. Completion enters a
reporting phase, as does budget exhaustion, with distinct outcomes. Goal report
inference failure falls back to factual durable progress. Saved answers and Slack
outbox intent commit together using the existing delivery/reconciliation path.

Validation: 23 goal/state tests passed; the fake-clock three-hour case replaces the
computer near the boundary and fails final generation while retaining a report.
Two gateway/Slack integration cases verify accounted independent review, failed
report inference, atomic report/outbox settlement and retry deduplication. Three
browser cases cover goal states at 1440, 768 and 320 pixels; rendered screenshots
were inspected. The broader runtime/gateway/Slack regression run passed 562 tests with 15
optional skips. Schema migration tests passed on PostgreSQL; all 33 then-current
goal/journal contract cases also passed with the PostgreSQL backend. This is not a real three-hour provider/deployment rehearsal.

Receipt-bound Slack controls and owner-bound initial non-chat inputs are supported.
Open within WP4: operational recovery qualification and the real three-hour rehearsal. The clock records active execution, not measured useful work;
a crash can lose the interval since its last successful receipt (normally two
seconds, longer during a broker outage). Periodic reporting independent of the stop
budget and richer source-backed review under extreme context pressure also remain.

## Coverage at proposal time

The statuses below are the original baseline deficiencies, not implementation
completion claims. Newer baseline commits already improve initial memory/skills,
GitHub CI, diagnostics, and API-only deployments; these are preserved.



“Open” means the limiting behavior remains in the reviewed source. “Partial” means useful infrastructure or a targeted fix exists, but the full finding is not resolved. Acceptance references point to the scenario suite; each package also includes deterministic contract tests.

| Finding | Current status and deficiency | Proposed owner/change | Acceptance |
| --- | --- | --- | --- |
| D01 | Open: peer bots dropped before useful participation | WP3: retain attributed bot source; independent response decision | S01, S25 |
| D02 | Open: initial Slack source frozen; omitted once history exists | WP3 + WP2: live watermarks, deltas, complete retrieval | S01, S02 |
| D03 | Open: observing text conflated with responding to it | WP3: source ingestion separated from wake/participation | S04, S25 |
| D04 | Open: five queued messages / 100 turns | WP1: durable logical intake, no arbitrary session count cap | S03 |
| D05 | Open: every steered message called a correction | WP3 + WP2: neutral attributed inputs and semantic interpretation | S04 |
| D06 | Partial: optional native state for some adapters; shared public replay still flattened | WP2: typed replay and qualified opaque native continuity | S05, S06 |
| D07 | Open: fixed previews/summary sizes and awkward full-history access | WP1 + WP2: durable full items, token-fit views, history tools | S02, S05 |
| D08 | Open: sustained natural language does not register `/goal`; wrapping can defeat parsing | WP4: shared goal tools and original-request control handling | S07 |
| D09 | Open: hidden one-hour/25-continuation goal limits | WP4: objective plus explicit floor/timebox/cap, durable report settlement | S08, S09, S12 |
| D10 | Open: completion evidence checks successful tool IDs, not outcome | WP4: independent source-backed requirement review | S10 |
| D11 | Open: search-gated/keyword memory, fixed selection, repository-scope mismatch | WP5: automatic scoped recall, complete retrieval, confirmed repo identity | S13 |
| D12 | Partial: loaded-skill cap removed; search/index/bundled-loader gaps remain | WP5: full discoverability, direct versioned loading and supporting files | S13, S14 |
| D13 | Open: procedural global prompt moved into Markdown but remains broad | WP5: small orientation prompt and tested reusable procedures | S13–S17 |
| D14 | Open: native capabilities disabled without complete parity; repository guidance loading gap | WP2 + WP5: native-feature qualification and one guidance resolver | S06, S14 |
| D15 | Targeted startup catalog fix already present; guard against regression | WP2 + WP6: qualified catalog/schema readiness and usable discovery | S06, S22 |
| D16 | Partial: settings/context accounting exist; effective runtime still insufficiently legible | WP2 + WP9: resolved provider/model/effort/limits/version receipts | S06, S24 |
| D17 | Open: broad compatibility assumed beyond demonstrated support | WP2 + WP6: tested model/harness/media capability matrix | S05, S06, S16 |
| D18 | Open: research connectors cannot exhaust later pages/nested content | WP6: complete paged and nested reads with coverage | S15 |
| D19 | Open: browser content/element truncation and narrow interactions | WP6: navigable extraction, stable targets, visual/media primitives | S16 |
| D20 | Partial: default PR guidance exists; workflow primitives remain incomplete | WP6: scoped standard Git/SDK workflow and real outcome checks | S17 |
| D21 | Open: archive/file/text caps lose child evidence and outputs | WP6: explicit versioned artifacts, streamed storage, range/binary access | S18 |
| D22 | Open: delegation suspends parent until group settlement | WP7: asynchronous handles, partial results/messages, explicit waits | S19 |
| D23 | Partial: memory/skill rejections improved; generic connector validation/uncertainty remains | WP6 + WP8: dispatch-aware structured outcomes and repair | S22, S23 |
| D24 | Partial: receipt/transport recovery exists; failures can still terminate useful work | WP8 + WP4: one recovery owner and durable objective/report state | S12, S23 |
| D25 | Partial: project environments exist; fan-out readiness/capability use remains weak | WP7: prepared versioned prerequisites and truthful capability context | S20 |
| D26 | Open: automation/delegation categorically denied schedule management | WP7: explicit owner-scoped self-management capability | S21 |
| D27 | Partial: delivery guidance exists; execution and delivered user outcome still diverge | WP4 + WP6 + WP9: saved result, artifact access, PR/UI evidence, delivery receipts | S08, S12, S16–S18 |
| D28 | Partial: linked-session read merged; full item/child/export/config visibility incomplete | WP1 + WP6 + WP9: full authorized evidence reads and provenance | S24 |
| D29 | Partial: shared production/eval lifecycle now fixed; small single-turn coverage remains | WP9 throughout: multi-turn/surface/recovery/evidence suite | S01–S26 |
| D30 | Open: failure-to-skill/prompt/replay/held-out improvement loop incomplete | WP5 + WP9: versioned incidents, common-layer repairs, held-out generalization | Replays plus held-out families |

The new three-hour complaint is covered by D08–D10, D24, and D27, with S08/S09/S12/S23 as explicit release gates. It is a lifecycle-and-delivery requirement, not merely a larger timeout setting.


## Slack observation and participation (WP3; not deployed)

Signed source events and complete attributed message versions now persist separately
from user inputs. Peer bots, humans, edits, deletions and reactions are retained.
Unmentioned activity coalesces wakeups, while direct inputs retain individual
receipts. Ambient silence is explicit; only an authorized human source can be
adopted as a requester. Direct requests and active goal reports cannot be silenced.
Own posts use installed identity or signed outgoing provenance to avoid reply loops.

Live backfill follows all cursors, retains partial progress and reports incomplete
coverage. Source versions support paging/ranges, survive computer replacement and
respect execution/installation fencing. Delayed older messages and reactions do not
overwrite newer source state. The shared source view carries a durable watermark;
private file-download locations stay in raw source storage and files use stable IDs.

Removed the frozen mention-time view, peer-message exclusions, 16K inbound cut,
50-message/three-page source cap and bounded attachment-text extraction. Updated
Slack docs and the existing source panel; the combined release migration is schema revision 8.

Validation: 516 broader Slack/journal cases passed, with four optional PostgreSQL
pool cases skipped. The remaining failure was an exact old backfill-parameter
fixture; its corrected focused case passes. Ten new source/collector contracts
pass against PostgreSQL, and all 20 migration cases pass against PostgreSQL.
Three browser cases pass at 1440/768/320, with the mobile screenshot inspected;
the UI bundle builds. These are local contract/fixture tests, not a live Slack
behavioral parity benchmark. Raw event retention/operational quotas, large source
view efficiency and the non-durable adapter context path remain release concerns.

## Memory, skills, and scoped guidance (WP5; not deployed)

Private libraries now preserve complete note bodies, skill instructions, supporting
files, and every file-read receipt. Empty-query enumeration and revision-bound
cursors expose the full authorized catalog. Exact revision/range reads recover
large bodies; there are no fixed library counts, body sizes, five-result search
caps, or four-excerpt retention caps. The inference compositor fits whole records
or explicit retrieval references to allocations derived from the actual model
input window; normal context admission still checks the complete request.

Standing preferences and task-relevant notes are recalled automatically. Other
notes remain discoverable with repository labels. A verified connected GitHub
repository ID can scope memory with an empty picker; connection access is checked
again at use. Private bodies and catalog metadata taint native checkpoints and
remain bound to the active requester and turn. Ambient Slack observations do not
become personal-memory or skill-selection authority.

The background memory reviewer processes complete original sources in model-sized
pages and may update only note bodies it saw. Malformed or duplicate proposals in
one response reject the review; later-source corrections can supersede earlier
pages. A thin orientation prompt points to seven public, content-versioned
procedures and one workspace-scoped AGENTS.md resolver. Public procedures work
on non-chat, automation, and child execution surfaces. New skills default to
Personal; organization publishing still requires explicit intent and an admin.

Validation: 387 library/gateway regression cases passed. Eleven discovery, large
range, revision, repository-access, and execution-surface contracts passed on
PostgreSQL. Revision 7 migration tests previously passed all 20 PostgreSQL cases.
Three synthetic browser cases verify complete note save/edit and large skill
imports at 1440/768/320; screenshots were inspected. The editors scroll long
bodies within the dialog. The UI build and 22 focused Node checks pass. Retrieval uses keyword
ranking with complete discovery/read access, not embeddings. Oversized initial
repository guidance and adapter qualification remain in WP2.

## Connector coverage and action errors (WP6, partial; not deployed)

Slack search, channel roots and thread replies; Linear teams, requester issues,
title search and comments; and Notion search and nested block reads now expose
complete provider pagination through `next_arguments`. Full provider bodies are
retained. Coverage distinguishes a final page from missing/repeated cursors and
labels the live, potentially changing source. Notion child collections can be
traversed explicitly. Provider permissions, indexing and retention still apply.

Argument failures return repairable field receipts without echoing private input.
Connector failures distinguish rejection, response loss, retry delays and unknown
write outcomes. No failed write is automatically replayed. Linear bodies are no
longer cut by local schema quotas. Slack and Notion send complete text in parts
at actual provider payload boundaries; partial failures expose acknowledged parts
and offsets, and stop on execution/connection changes. Legacy GitHub failures
without dispatch evidence remain conservatively unknown for writes.

Validation: 186 connector, Slack sender, workspace and automation regressions pass,
including multi-page and nested research, long Unicode append, complete threaded
Slack send, argument privacy, response loss and cancellation between write batches.
These tests use simulated provider HTTP responses through the real broker. Browser,
GitHub workflow/binary support, durable artifacts and broader recovery remain open.

## Browser observations and actions (WP6, partial; not deployed)

Browser observations retain full frame text and all discovered controls, including
open shadow roots. Optional ranges refer to an immutable observation with a
timestamp. Frame and tab directories expose navigation; exact DOM handles prevent
actions from silently selecting a different element after a rerender. Changed or
detached targets require a fresh observation. Role/label and coordinate actions
remain available, with select controls, complete text insertion, workspace uploads,
download completion/path receipts, back/forward/reload, and tab selection. Named
screenshots return MCP image content as well as their saved path.

Validation: 200 existing computer/MCP regressions passed. Four additional contracts
passed using real local Chromium for long Unicode observations, controls beyond
the old cutoff, nested frames, open shadow roots, stale target rejection, complete
form input, select/upload/download, and tabs; one case verifies MCP image bytes.
The browser cases require Playwright and an installed Chromium (`MOYAI_TEST_CHROMIUM`
can select it); the initial invocation lacked the matching default browser, then
passed against the installed binary. This is local protocol/behavior verification,
not deployed browser or model/media qualification. Capture/checkpoint storage
quotas and explicit durable artifact publication remain open in WP6/WP2.


## Durable artifacts and complete recovery files (WP6; not deployed)

Explicit publication now streams complete workspace files in transport parts. The
server verifies the full size and SHA-256 before atomically publishing an immutable
revision and Files-refresh event. Upload/part identities survive acknowledgment
loss. Capability, turn and requester changes reject stale publication. Original
revisions remain readable after later edits; current/direct-child reads support
byte ranges and verified local fetch. Authenticated downloads stream independently
of the recovery ZIP. Text previews continue across UTF-8 boundaries with Load more.

Recovery collection removes file, total-byte, file-count, directory-count and
repository-count omission caps. Files and child readers retain complete catalogs,
range/binary access and full child answers. Archives use streamed server reads and
sandbox-to-local transfer; dependencies, hidden files and symlinks remain excluded.
Child handoffs freeze published revision manifests alongside legacy archive versions.
Storage maintenance inventories, migrates and verifies published chunk references.
Unfinished upload parts retain the same session/database retention as other saved
source data; no separate garbage collector deletes potentially recoverable uploads.

Validation: 177 artifact, child, storage, workspace and provider regressions pass.
Fourteen initial publication contracts pass on PostgreSQL; all 20 revision 8
migration checks pass. Eight Files Node checks and three browser cases pass at
1440/768/320, including a session without a ZIP and complete preview continuation;
the mobile screenshot was inspected. The 31 final artifact/Files checks also pass, including HTTP/catalog, revision
discovery, chunk migration and raster previews. All 17 publication contracts pass
on PostgreSQL after those additions; the UI bundle builds. Provider transfer limits and image-decoder safety bounds still apply;
there is no local total-file retention limit on explicitly published outputs.

## Response latency acceptance clarification

The October 10 Slack example asks why an informational question takes so long.
The user clarified this concerns mandatory computer startup, **not adding LiteLLM
key-creation MCP tools**. WP8/S26 must exercise the actual selected agent answering
and using eligible connector tools before a computer is acquired, then continuing
through the same durable turn when a shell/browser/repository tool requires one.
Measure input receipt → first inference → first useful response/action, with cold
computer provisioning held. No separate greeter, canned acknowledgment or changed
model counts as passing this scenario. This runtime change has not landed yet.

## Integration with current main

The October 10 integration preserves AgentSwarm, fast conversation navigation,
instant queued-message updates and rolling-release ownership. Child handoffs keep
the new harness/model identities alongside complete answers and frozen artifact
revisions. The shared swarm artifact contract now supports revision and byte-range
reads. This schema/protocol change needs the coordinated migration path; it is not
a compatible rolling or API-only release. Pushing source does not deploy it.

Integration validation: 538 Node tests, 18 browser cases, the UI build and
typecheck pass. The 225-case focused backend run initially found two obsolete
limit/ranking expectations; the affected initial-context and swarm suites pass
all 56 cases after correcting those expectations. PostgreSQL passes 96
migration/rolling-owner checks and 74 goal/journal/source/library/artifact
contracts. The broad repository regression completed with 4,970 passes, 94 failures and
five setup errors. Follow-up contract repairs passed 226 cases, startup/image
checks passed 75, and the remaining selected contracts passed 334; the final
evaluation fix passed 36. A fresh full run is still required. Provider and
production rehearsals remain separate release gates.

## Shared computer primitives (WP6/WP8 prerequisite)

The MCP catalog and in-process harness bindings share complete byte/range file
reads, atomic text/binary writes and persistent command handles. Removed the
100 KB output/read cutoffs and 120-second command lifetime. Launch identities
prevent duplicate execution after a lost acknowledgment; unavailable supervisors
report unknown outcomes. Explicit stop kills the process group, including a
command ignoring TERM. Input backpressure returns an acknowledged byte prefix
and exact remaining suffix. Appending logs remain readable without pretending
that the file is immutable. Logs/arguments live outside Git working trees.

Validation: 183 computer/MCP/harness cases passed with 15 optional runtime skips;
all eight new computer contracts passed after the log-location follow-up; the
final MCP suite passes all 32 cases. The live Slack-source regression expectation
now preserves the current attributed reference. Independent agent runtime and
lazy computer acquisition are still in progress and are not enabled by this
prerequisite.

## Model-sized cold replay (WP2)

Cold starts now derive their history and current-request views from the selected
model's verified input window. Complete scrubbed typed records remain in immutable,
content-addressed JSONL references outside repositories, in addition to the server
journal. Earlier references stay valid as later views are created. Fresh child
or stale-workspace resets remove inherited references. Codex receives typed replay;
adapters without a public replay API retain a labeled reference seed. A native
resume does not redundantly inject old history. These conservative UTF-8 allocations
are views, not retention limits or optimal token packing.

Library allocation now resolves capacity without counting a preliminary empty
request. It honors the actual request's output allowance; admission still measures
the complete request after tool schemas, private context and media are present.

Validation: 294 context, gateway, library and real Claude SDK transport cases pass,
including all 30 SDK/model-routing/recovery fixture combinations. Another 52
allocation/lifecycle cases pass; the broader run passed 276 cases and its 15 stale
typed-replay assertions were corrected and covered by that 52-case rerun. The SDK
tests use simulated inference responses, not live model-quality comparisons.
Reference availability across future runtime/filesystem changes and typed replay
qualification beyond Codex remain explicit WP2/WP8 work.


## Independent runtime and complete native state (WP2/WP8; opt-in)

Codex on Modal can enter the same shared conversation lifecycle in a separate
control guest, without repository checkout, desktop preparation or attachment
download. Broker tools remain available before a full computer is acquired.
Computer tools use stable operation identities and the existing durable controller;
UI wake can request the same computer. The model, turn, goal and capability are
preserved. App-only answers settle without a filesystem snapshot. The feature
is disabled by default pending staging/provider qualification; see
[independent runtime](independent-runtime.md).

Preparation, tools and finalization use the existing launch-once supervisor.
Launch specifications are immutable; segment/generation identities distinguish
new computer preparation and saves from earlier snapshots. Project interpreters
and the credential proxy are activated for each tool without restarting project
services. Worker retry, owner changes and machine loss preserve completed receipts
and distinguish rejected actions from unknown dispatched effects. Cleanup closes
pending receipts without claiming that an uncertain write failed or succeeded.

Removed the native-state 2 MB/256-file cutoffs and Claude transcript abandonment
at that boundary. Large native state and computer requests use one encrypted,
owner-bound multipart transport. Integrity, destination and execution ownership
are checked before the original endpoint validates the assembled body. Native
checkpoint, taint and revocation checks remain. Complete broker read/control
responses no longer fail at an unrelated 5 MB response cutoff. Stored payload
parts currently share session retention; storage governance remains operational work.

Validation so far: 314 runtime/context/environment cases and all 24 PostgreSQL
migration cases pass. The real pinned Codex SDK with synthetic inference reads a
broker result before computer admission, then writes through the computer bridge
in the same native thread/model. That test found and corrected a real MCP approval
configuration mismatch in the read-only control guest. Another 109 transport/
recovery cases, 330 adapter/configuration cases (11 optional skips), 23
PostgreSQL operation/payload cases and 11 image checks pass. A Python 3.14
isolated environment with exactly the declared lightweight image packages imports
the guest and verifies its pinned Codex dependencies. The operator guide was
rendered at desktop/mobile widths and local links checked. This is protocol evidence,
not measured deployed latency or model quality. Broader recovery, other harnesses
and providers, and live S26 qualification remain open.

Current source requires schema revision 9 and all four role protocol revisions 4.
The previously pushed foundation remains revision 8; this increment adds computer
operations and encrypted payload parts. It requires a coordinated migration.


## Asynchronous delegation and attributed follow-ups (WP7; not deployed)

`swarm_start`, `agents_fanout` and retries now return child handles after admission
and snapshot creation. Parents can work independently and launch multiple groups.
`agents_wait` reads changes with a cursor or explicitly checkpoints/releases the
parent until settlement. Reading settled results freezes the handoff; uncollected
groups still prevent premature parent completion. Direct child guidance is
idempotent, steers active work, wakes idle work, and reopens collected groups when
the parent requests refinement. Selected children can be cancelled independently.
All of this uses Ishaan's existing AgentSwarm interface, database and workflows.

New child assignments, retries and board messages retain explicit agent provenance
in storage, live steering, native inputs and cold fallback. They cannot become
new human goal commands. Dispatch after an awaited snapshot, long waits, shared
board writes and Python handles retain the original actor/turn/capability fence.
No arbitrary prompt, roster, item-list, board-message or child-answer clipping
remains in these delegation contracts. Configured workspace capacity still applies;
`MAX_PARALLEL_AGENTS` accepts any positive operator value. Large tool arguments
use the same authenticated multipart transport as native state and computer calls.

Validation: 389 broader delegation, swarm, steering, goal, MCP and broker checks
pass; all 68 database-dependent role checks also pass in a separate PostgreSQL
invocation. Another 76 new-contract cases and 96 source/fallback cases
pass. Forty-two delegation/board/transport cases pass against PostgreSQL. The
AgentSwarm guide was rendered at desktop/mobile widths and inspected. This is
local protocol and behavioral evidence with simulated workers, not a live
model-quality benchmark or a deployment. Full-repository regression, independent
prerequisite readiness and owner-granted automation self-management remain open.

The schema remains revision 9. All four role protocols are now revision 4 because
older workers interpret agent-generated inputs as human messages. Drain old roles
and use a coordinated release; rebuilding only the broker is not sufficient.

## Complete automation history and scoped session reads (WP2/WP7; not deployed)

Automated work receives a complete history reference rather than three summaries
clipped to 1,000 characters. `automation_history` returns full public answers using
keyset cursors, including tied timestamps. `sessions_read` now permits delegated
work to inspect its session family, and automation descendants to inspect prior
runs of their own automation. Every message includes its input kind and provenance.
Unrelated session search still requires a direct authenticated chat; active
requester, turn, capability and deletion checks protect reads across awaits.

Settings history can load earlier runs and retains loaded rows during refresh.
The editor preserves complete instructions and metadata and permits more than
twenty triggers or metadata entries. Saved workflows have no fixed count ceiling.
Configured invocation and execution capacity still apply.

Validation: 272 broader automation, session, workspace and delegation cases passed;
two catalog failures exposed an incorrect removed import and were corrected.
The apparent duplicate `READ_TOOL` names were different tools (`history_read` and
`sessions_read`); both remain available. Sixty-four contracts passed on PostgreSQL,
sixteen Node cases passed, and the real UI with synthetic API fixtures passed at
1440/768/320 pixels, including 26 history entries and complete editor saves.
Rendered automation documentation passed desktop/mobile overflow checks.
The frozen runtime-foundation suite finished with 5,114 passes, 296 skips, and one
fixture failure already repaired and covered by the delegation suite.

Owner-granted automation self-management remains the next implementation step.
These are source and local behavioral checks, not deployment or model-quality proof.

Integration with the latest renewable-swarm and shared-chat changes passed 96
Python cases (one optional skip) and 40 Node/browser cases. The two corrected
catalog cases and schema manifest check also passed. Main now requires schema 10
(shared swarm chat) and role protocols 6 (complete automation-history references).
Use a coordinated release of all roles; no deployment was performed.

## Owner-granted automation self-management (WP7/S21; not deployed)

Owners can select instruction and existing-schedule permissions in the editor or
the saved definition. Automated runs and their descendants inspect the current
grant with `automation_self_read`, then use `automation_self_update` to change
only those fields. Scoped edits retain the current pause state, existing trigger
IDs, event sources, connections, runtime, ownership and budgets. They use the
existing scheduler and mutation receipts; concurrent owner edits produce a
revision conflict, and retries cannot create another schedule.

A direct human chat can grant the same fields to its workers with
`automation_grant`. The durable grant references the authorizing owner, run and
input; reads and writes recheck that source, active turn, requester, execution
and automation revision. Parent waiting preserves the grant; a new turn,
requester, stop, deletion or explicit revocation removes it. Granted workers can
read the selected workflow's complete history and linked sessions. Agent and
event inputs cannot mint grants or expand them.

Scheduled inputs now carry explicit automation provenance and the immutable saved
instructions. The UI labels them Scheduled workflow. Goal authorization reads
only those owner-authored instructions, excluding event/history data in the
rendered prompt, while ordinary human follow-ups retain their own attribution.

Validation: 98 management/history cases and 276 broader automation, event, goal,
session and package cases passed. Fifty-one history/permission contracts passed
against PostgreSQL and 76 migration/role checks passed. Three additional
revocation/provenance/manifest checks passed. The real local Temporal scheduler
confirmed a scoped timezone/time change, retained exactly one schedule and
reconciled the identical retry. Eleven Node/browser checks passed, including
editing both permission fields at 1440/768/320 pixels. The rendered operator and
automation guides passed desktop/mobile checks. Tests use synthetic account and
provider data; deployed provider quality and latency remain unqualified.

Current source requires schema 11 and all four role protocols 7. Earlier schema
digests are preserved; revision 11 adds owner grants. Release all roles together
after the offline migration. No deployment was performed.

## Complete GitHub PR and CI reads (WP6; not deployed)

The agent and native PR panel can page through every provider-returned file.
Descriptions, comments and patches no longer lose text at local character limits.
Head/base SHA checks protect every page, including a second check after fetching
files, from mixed-revision evidence. The panel retains loaded pages on tab return
and clears them after a revision/access conflict. Missing patches and incomplete
provider file lists have explicit coverage and a checkout fallback.

CI metadata retains all supplied step names and steps. Job logs no longer stop at
2 MiB or omit long lines at 20,000 characters. Positive line/page parameters have
no arbitrary upper cap. Redaction runs over the complete downloaded source before
line paging; continuation arguments bind its digest, and changed logs require a
fresh read. Provider access and credential-free redirect checks remain in place.
The current implementation downloads and redacts the full log for each page;
large-log memory/latency optimization remains possible without sacrificing coverage.

Validation: 141 GitHub read, delivery and follow-up cases passed; the new seven
source-completeness/revision contracts passed after correcting their fixture
import. Another 66 read/delivery cases passed. Thirty-eight Node/browser cases
passed, including 101 loaded files, a 25,500-character patch line, complete
descriptions and changed-source clearing at 1440/768/320 pixels. These checks use
synthetic GitHub responses. Provider patch/file limits remain provider limits;
the returned patches are not proof of a complete local diff.

The schema remains 11; all role protocols are 8 so mixed brokers do not disagree
about paged read and source-revision arguments. Publication file/binary limits
remain the next GitHub implementation step.

## Complete text and binary publication (WP6; not deployed)

The existing scoped GitHub publisher accepts regular text and binary files,
complete PR descriptions and comments, and arbitrary changed-file counts.
Removed the 100-file, 10 MiB per-file and 20 MiB total publication ceilings.
Binary content uses canonical base64 Git blobs; existing text payloads retain
their exact receipt serialization so old uncertain writes still reconcile.
Large requests use the existing authenticated multipart transport. Each envelope
has a framing limit, without imposing a total-content limit. GitHub's own API
constraints and protected-path, exact-base, ownership and non-forced-write
checks still apply.

Validation: 234 broader GitHub and transport cases passed; the remaining test
was updated to exercise multipart across the real loopback relay and passes.
Nineteen payload/package cases and nineteen final binary/checkout/relay cases
passed, including complete bytes after a fresh Git checkout, more than 100 files,
and one publication after a lost acknowledgment. Ten publication/payload cases
passed against PostgreSQL. Integration with main's atomic startup-index fix
passed 65 migration/admission/runtime cases; the ownership test now waits for
PostgreSQL to complete backend termination before asserting its fence.
The import check also passed with the optional migration dependency, for 66
passing integration cases. The integration guide was rendered at desktop/mobile widths and
inspected. All provider responses in these checks are synthetic.

Current source requires schema 12 (main's startup fix) and role protocols 9.
No deployment was performed.
