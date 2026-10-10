# Intelligence implementation ledger

Status: in progress on `codex/moyai-intelligence`, integrated with `bb9eba8` from `main`.
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
contracts. The full repository regression is still running; provider and
production rehearsals remain separate release gates.
