# Chat database performance

Status: proposed changes, not deployed. This patch reduces database work for
opening a native chat and loading its workspace:

- `GET /api/sessions/{id}?resolve=true&include_activity=false&include_messages=true`
- `GET /api/sessions/{id}/workspace`
- Transcript pagination uses the same narrower session authorization read.

## What changes

1. Resolve each distinct turn author's relationship to the viewer once within
   the existing projection transaction. A linked Slack author can appear on
   every turn. Repeating canonical admission and identity reads for that author
   previously added hundreds of SQL round trips.
2. Omit private conversation and snapshot fields from session metadata and
   authorization reads. A waiting chat explicitly loads its conversation to
   preserve questions, approvals and credential-request behavior. Execution
   callers retain the default full row. Omitted attributes raise if accidentally
   accessed; they cannot trigger hidden lazy reads.
3. Select only turn ID, author and status for workspace lineage. Keep the same
   recent-turn limit, ordering and active-turn inclusion.
4. Use the existing batched public-message projection for workspace PR mentions,
   and batch recorded history-segment reads across its authorized turns.

Response schemas, pagination, source visibility, identity checks, continuation
fences and final revalidation remain in place. Relationships are not cached
across requests. There is no migration, index, dependency or frontend change.

## Local measurements

PostgreSQL 16.15, actual canonical/native identity queries, signed-in HTTPX ASGI
requests, deterministic Slack membership responses, one warm-up and seven
measured repetitions per version. The comparison loads the original modules
from main commit `1b9c1a2d` and checks complete HTTP JSON equality. The earlier
benchmark base `eeb05e4d` has identical versions of all five measured modules.

The combined measurement runs chat then workspace, matching their current
dependency. It includes authentication, authorization, query execution, native
store lookups, decryption and response serialization. It excludes browser and
network transport, rendering, connections, mark-read, SSE and Activity requests.
It therefore measures these two backend loads, not complete page readiness.

| Synthetic case | Combined before | After | Speedup | SQL statements before → after |
| --- | ---: | ---: | ---: | ---: |
| Web: 1 turn, 2 messages, large checkpoint | 47.91 ms | 33.61 ms | 1.4× | 99 → 98 |
| Linked Slack: 1 turn, 2 messages, large checkpoint | 63.52 ms | 47.36 ms | 1.3× | 194 → 185 |
| Web: 20 turns, 40 messages | 39.82 ms | 32.02 ms | 1.2× | 136 → 97 |
| Linked Slack: 21 turns, 41 messages | 65.75 ms | 45.37 ms | 1.4× | 392 → 184 |
| Linked Slack: 101 turns, 201 messages, large checkpoints | 194.97 ms | 50.00 ms | 3.9× | 1,103 → 185 |

The large case returns the unchanged first page of 100 turns and 200 messages;
workspace examines the newest 20 prepared turns. Each synthetic checkpoint is
approximately 2.24 MB of encrypted text. The Slack fixture includes its original
ingress turn, which accounts for the extra turn in the 21/101-turn cases.

For the large case, chat alone falls from **131.43 to 33.15 ms** and workspace
from **58.25 to 16.24 ms**. Combined measured SQL time falls from **96.34 to
14.37 ms**. SQL counts/times cover the native and canonical SQLAlchemy engines;
direct Harness-store queries are included in HTTP time but not those counters.
Combined medians need not equal the sum of endpoint medians.

Small timing differences are sensitive to local scheduling. The web-chat query
count itself does not improve. These results do not establish a universal 10×
gain or a production browser speedup.

## Production-data checks

Bounded, read-only queries compared old and new column projections on four
existing chats in one repeatable-read snapshot. Every retained session field,
workspace turn field, row order, and per-turn history-segment sequence matched.
The sample contained six prepared turns and three history-segment events.
Only aggregate counts, sizes and timings were retained as evidence.

The reported slow chat has one turn and two visible messages, but its stored
conversation is **2,259,084 bytes**. Each narrower session read omits **2,259,599
bytes**, and its workspace turn projection omits **2,271,218 bytes**. In the final
three-pair sample, session SQL plus fetch fell from **35.70 to 3.68 ms**, and
workspace-turn SQL plus fetch from **23.35 to 5.74 ms**. These small samples
validate a component improvement; they do not explain the original five-second
request. No candidate API was started against production, and full production
HTTP response equivalence was not measured.

The existing production browser capture measured a **5.03 s** initial chat load,
followed by a **1.99 s** workspace request that finished at roughly **7.05 s**.
The chat response was only about 5 KB. Slack-provider waits, canonical lock
contention, connection checkout and other concurrent requests remain possible
contributors. The independent authorization-stall work in PR #637 addresses
provider waits inside admission transactions; this patch does not change that
locking behavior or replace that work.

## Reproduce and verify

From the repository root, use local PostgreSQL with an account allowed to create
disposable databases. The fixtures reject non-local hosts and remove only their
own databases. Fetch main history first if using a shallow checkout, so the
pinned baseline commit is available.

```sh
MOYAI_TEST_POSTGRES=postgresql://localhost/postgres \
MOYAI_BENCHMARK_CHAT=1 \
uv run pytest -c native/pyproject.toml native/tests/test_chat_database.py -q -s
```

Expect passing tests and five `CHAT_DATABASE` records with before/after HTTP,
SQL and decryption timings. Each benchmark asserts exact response equality.
Run this comparison without concurrent CPU-heavy tests.

New regressions cover bounded query growth, large checkpoints, mixed authors,
pagination without gaps or duplicates, loss of private access, stale Slack
profiles, disabled senders, membership revocation, and batched lineage ordering,
deduplication and scope. Existing workspace, continuation, swarm, credential,
message-source and one-connection-pool suites cover the surrounding boundaries,
including revocation after asynchronous reads and pending questions/approvals.

After deployment, measure the same production chat through initial transcript,
workspace, scoped connections and Activity readiness. Compare ordinary and slow
samples separately. Any remaining multi-second delay needs server timings for
database checkout, authorization/provider waits, SQL, native-store reads and
serialization before claiming the complete loading problem is solved.
