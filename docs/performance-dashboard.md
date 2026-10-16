# Admin performance dashboard

Open **Settings → Administration → Performance** (`#performance`). The page and
`GET /api/admin/performance` require an organization administrator. The response
is uncached and contains request IDs, model names, states and timings, without
prompts, responses, tool contents or account identifiers.

| Measurement | Start | End |
| --- | --- | --- |
| Startup to dispatch | Entry into the valid web submission handler | First foreground outbound model HTTP attempt starts |
| Queue pickup | Web submission handler entry | Turn claim |
| Worker to broker | Turn claim | Broker ingress of that first dispatched attempt |
| Broker preparation | Broker ingress | Outbound model HTTP attempt starts |
| Computer readiness | First accepted computer-tool request in the turn | Usable independent-runtime computer |
| Send now pickup | Explicit Send now or automatic immediate handler entry | Steering acknowledgement, or claim as a new turn |
| Startup to admission (historical) | Valid web submission handler entry | First model request registered at the broker |
| After pickup to admission (historical) | Turn claim | First model request registered at the broker |

These are server-observed intervals, not click-to-render, time to first token, or
time to a visible answer. They include database, queue, execution startup and
model-admission waits after handler entry, but not request parsing, uploads,
browser-to-server transport, provider response time or browser rendering.
Background memory/skill learning and private compaction do not start the model
clock. A new conversation does not necessarily mean a cold sandbox.

The report covers main web conversations using cloud execution, across sandbox
providers and harnesses. It excludes subagents, local demo execution, internal
metadata messages, deleted messages/sessions and submissions from Slack or
schedulers. A web Send now action on an older queued message can start a new
pickup sample without fabricating its original submission time. Injected messages
are excluded from startup; they contribute to Send now. Model grouping uses the
selected message model, not a subsequently resolved provider alias.

Measurements are persisted in `performance_samples` within the same transactions
as submission, claim, acknowledgement and first model-request registration.
Retries and repeated clicks retain the first timestamps. Transaction rollback
cannot leave a successful timing receipt. Milestones are durable across restarts.
They use UTC wall clocks across services, so synchronized host clocks are needed;
negative or invalid intervals are excluded and reported as invalid.

Averages, median, p95 (nearest rank), maximum and measured counts use observed
endpoints, even if execution subsequently fails. Waiting requests and ended
requests missing an endpoint are reported separately, never as zero latency.
Percentiles from small samples can fluctuate, and waiting requests mean the
completed-sample average does not describe the eventual latency of all arrivals.
The after-pickup card has only claimed messages in its cohort.

Filters cover 1 hour, 24 hours or 7 days, new chats/follow-ups, selected model, and
explicit/automatic Send now. The Send now filter affects only pickup measurements;
startup retains its own submission cohort. Trends have 24 equal intervals with
missing intervals shown as gaps. Recent requests link to their conversations.
Refresh is manual and retains filters and expanded methodology/trend details.

Reads run off the event loop, in a read-only transaction, with indexed time ranges.
Each cohort materializes at most 10,000 eligible samples plus one truncation
sentinel. Large windows display an explicit warning and use the latest samples;
choose a shorter period for full coverage. No full-history backfill is performed.
Rows follow the lifetime of their message/session through cascading foreign keys;
soft-deleted rows are excluded from reports. No separate automatic retention task
is introduced.

## Outbound dispatch and computer readiness (schema 21)

The headline **Startup to dispatch** measures web handler entry to the instant
before Moyai invokes its HTTP client's first foreground model send. It includes
request construction and pre-send persistence/authorization; it excludes connection
setup, transport, gateway receipt, and inference. Both Chat Completions and native
Responses/Messages routes record this boundary. Compaction and internal assessment
requests do not qualify. An observed failed/cancelled HTTP attempt still counts;
validation failures before send have no dispatch timestamp.

The original `first_model_at` admission endpoint and API metric keys remain unchanged,
under **Model admission timing (historical definition)**. New fields are nullable:
`timing_version`, `dispatch_at`, `dispatch_broker_at`, `dispatch_request_id`,
`computer_requested_at`, `computer_ready_at`, and `computer_start_kind`. New web
submissions use timing version 2; historical rows are excluded from dispatch
coverage unless a real dispatch is subsequently observed. There is no timestamp
backfill. During mixed-version overlap old writers continue to work normally.

Dispatch timestamps are captured synchronously at send and persisted off the event
loop without placing another database write before network dispatch. Normal request
cleanup joins that observation; telemetry failures are logged without changing the
model outcome. A killed broker can leave a missing observation. The first observed
attempt wins by timestamp, with its own broker-ingress timestamp and request ID;
late persistence cannot attach a different retry's ingress or a later turn's identity.
Broker ingress is captured by server middleware, never supplied by a client header.

The breakdown uses one identical, ordered cohort:

- **Queue:** API handler entry to turn claim.
- **Worker to broker:** turn claim to the selected model attempt's broker ingress.
  This includes preparation, workflow/activity scheduling, retry/backoff before that
  attempt, and transport through the configured ingress; it is not worker CPU time.
- **Broker preparation:** that ingress to outbound dispatch, including context,
  compaction/admission and pre-send database work. It excludes the upstream stream.

Phase averages share a cohort and add to that cohort's startup average, subject to
rounding. Phase percentiles do not add. The headline can have a larger cohort when
an earlier phase is missing; coverage is shown. Cross-host timestamps require clock
synchronization. Invalid ordering is excluded, never presented as zero latency.

Computer timing covers the first accepted computer-tool request per measured web
turn using the independent runtime (including the default Deep Agents path). Its
start commits with the accepted operation, before acquisition/admission. Its end is
the worker's usable workspace boundary after runtime refresh and project startup/
verification, or a verified already-running computer. A project needing repair
cannot publish readiness. Cold start (base or prepared image), session snapshot
restore, and running-computer reuse are reported separately. Repeated tool calls
never replace the first acquisition. Tool-free turns, legacy non-independent
harnesses and idle manual computer wakes have no sample; they are not zero-latency
observations. A computer can start after model dispatch; these intervals must not
be added to startup. The current schema records first acquisition, not every
replacement within a long turn.

The reviewed online migration adds seven nullable columns from schema 20 to 21,
retaining compatibility revision 20 and existing role protocols. It does not provide
a migration from an earlier compatibility baseline: first complete the base release's
required transition. Once schema 20 is active, use the existing rolling release
procedure and its declared additive plan. The UI tolerates an older API response
without inventing dispatch data.

## Deployment order

**Merge after the ongoing schema-15 production adoption is complete.** Main now
also includes schema 16 from #376; this PR rebases on that change and introduces
**schema 17** for the additive timing table and indexes. Coordinate the final
16/17 release sequence with the deployment owner. This PR does not modify
release controllers, feature switches, concurrency limits or live services.

Use the existing offline migration procedure with the schema-17 release before
starting its API/coordinator, broker and workers in verify mode. Do not mix schema
versions across a live cluster or run DDL from a request handler. The dashboard
starts empty and fills with newly observed web requests after all participating
services are upgraded. Existing messages are intentionally not backfilled.

## Reproducible local demo and checks

```sh
uv sync --frozen --group dev
uv run python scripts/performance_ui_demo.py --port 8852
```

Open `http://127.0.0.1:8852/demo/performance-login`. This localhost-only demo uses
the real UI, admin session/API and SQLite report over clearly labelled synthetic
historical timings. It cannot start background jobs or call models/cloud services.
It does not demonstrate production latency or Google SSO. Stop it to discard its
temporary database.

```sh
uv run pytest -q tests/test_performance.py tests/test_message_queue.py tests/test_spend.py
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run pytest --postgres-backend -q tests/test_performance.py
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run pytest -q tests/test_schema_migrations.py
npm run typecheck
npm run build
npm test
node --test tests/browser/performance.cjs
```

Use only a disposable PostgreSQL database for tests. Tests cover real HTTP
submission/retry/Send now and broker paths with a synthetic provider, delayed
acknowledgement, next-turn pickup, access control, queue transaction rollback,
reads during an outstanding writer, missing/invalid timestamps, bounded reports,
upgrade without backfill, no-DDL construction, responsive controls, errors and
late navigation responses. The browser fixture server also supports this page
at `#performance`, including `?fixture=empty`, `error`, and `member`.
