# Admin performance dashboard

Open **Settings → Administration → Performance** (`#performance`). The page and
`GET /api/admin/performance` require an organization administrator. The response
is uncached and contains request IDs, model names, states and timings, without
prompts, responses, tool contents or account identifiers.

| Measurement | Start | End |
| --- | --- | --- |
| Startup to model | Entry into the valid web submission handler | First agent model request recorded at the broker, after admission |
| Send now pickup | Entry into the explicit Send now handler, or automatic immediate submission | Agent steering acknowledgement, or claim as a new turn |
| Queue pickup | Web submission handler entry | Turn claim |
| After pickup | Turn claim | First agent model request recorded at the broker |

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
