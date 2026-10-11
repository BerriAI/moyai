# Repository context

Moyai prepares a small, source-backed guide for connected GitHub repositories in
the background. A new task can use the guide before its computer or clone is
ready. This is a shared runtime service that skills and future capability
plugins can use; it does not change the skill package format.

The guide contains directory counts and excerpts from existing README,
architecture, contributor, testing, agent guidance and build-manifest files.
Every source link names an immutable commit. These are repository facts and
reference material, not new platform instructions. There is no generated
architecture claim or additional model call.

## What happens when

| Stage | Work |
| --- | --- |
| Background | Check each connected repository's default-branch head. On changes, index file paths and selected orientation documents. |
| First inference | Read a small encrypted guide from the database, check current authorization, and fit it into the shared model context budget. |
| Agent searches | `repository_context_search` ranks cached paths and orientation-document text. Follow its cursor with the same query and commit. |
| Agent reads | `repository_context_read` returns a line range from a cached document or fetches one source blob from GitHub at the selected commit. |
| Next turn | Select the latest available snapshot. The current turn keeps its selected commit even if a background refresh finishes. |

The initial view does not fetch GitHub, invoke a model, start a process, or clone
a repository. A missing guide returns immediately with no extra context; the
agent can use its normal checkout tools. A stalled refresh does not hold up
inference. Sessions without GitHub enabled skip this context service without a
database read.

Search is lexical over file paths and selected documentation, not semantic or
full-source search. Source bodies are retrieved on demand. Relevant historical
decisions remain available through existing personal memory, and PR details
through the existing GitHub tools. This change does not build a plugin loader,
marketplace, or a second memory-learning service.

## Freshness, limits, and failures

`REPOSITORY_CONTEXT_ENABLED` defaults to `true`. Set it to `false` to disable
indexing and injection. `REPOSITORY_CONTEXT_REFRESH_SECONDS` defaults to 900
(15 minutes). The worker checks for newly connected repositories every five
seconds when idle. Refreshes run sequentially, with up to four document reads
at once and a 60-second timeout per repository. Failures retain the last
successful snapshot and retry on the refresh interval. Incomplete document
fetches can be repaired at the same commit.

The index retains up to 20,000 paths and 20 orientation documents of at most
64 KB each. GitHub tree truncation and index limits are reported explicitly.
The initial guide has a 6 KB content budget before metadata and shared model
budget fitting; omitted documents remain searchable/readable. Source downloads
are limited to 256 KB of UTF-8 text; each excerpt is at most 200 lines and
12 KB. Larger, binary, symlinked or unindexed files require normal checkout
inspection. A line exceeding the excerpt limit reports `line_too_long`; read
that source in the checkout instead of repeating the same range.

The guide identifies its default branch, commit and indexing time. A working
branch or PR can differ: the agent must verify current files before editing.
The guide is an orientation aid, not an assertion that the checkout is current.

## Authorization and lifecycle

Repository IDs and the current connection fingerprint scope every snapshot.
Both inference injection and tool reads recheck the live session, requester,
GitHub connection policy and selected repository authorization. Installation
credentials remain in the existing broker. Disabling access blocks cached
content immediately; reconnecting requires a new authorized index.

Guide, document and path-index bodies are encrypted at rest. Automatic guide
injection uses the existing private-context marker and native-session reuse
rules. Source excerpts explicitly requested by the agent use the normal GitHub
tool-output boundary. This change does not relax conversation privacy to gain
speed or guarantee provider prompt-cache hits.

The inference owner runs background refreshes. During overlapping PostgreSQL
broker releases, the existing maintenance leadership lock admits one refresher
and shutdown drains it before ownership transfers. Schema revision 14 adds the
snapshot, head and turn-selection tables; run the normal offline migration
before a verified deployment. No migration is run by joining broker replicas.

## Verify and demonstrate

```sh
uv run --frozen --python 3.13 pytest -q tests/test_repository_context.py tests/test_initial_context.py
MOYAI_TEST_POSTGRES_URL=postgresql:///postgres uv run --frozen --python 3.13 pytest --postgres-backend -q tests/test_repository_context.py
MOYAI_TEST_POSTGRES_URL=postgresql:///postgres uv run --frozen --python 3.13 pytest -q tests/test_schema_migrations.py tests/test_broker_overlap.py
uv run --frozen --python 3.13 python -m scripts.repository_context_demo --repo BerriAI/moyai --port 8845
```

The localhost demo uses the actual context service and database with live,
read-only GitHub responses through the local `gh` login. It creates an isolated
temporary Moyai configuration; it never changes a production connection. It
measures guide assembly, source discovery, source reading and cache revocation.
It does not invoke a model or demonstrate an end-to-end task-quality gain.
Compare first useful action and completion time in a separate paired model
evaluation before claiming an overall latency or quality improvement.
