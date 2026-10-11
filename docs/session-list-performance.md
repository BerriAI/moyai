# Session list loading

The sidebar requests `/api/runs?scope=mine&view=sidebar` (or `scope=all` for
administrators). The compact view retains titles, original prompts, status,
timestamps, hierarchy, personal metadata, folders and PR summaries. Saved answers,
errors and sandbox details are read when a conversation opens. The default
`view=full` response and session detail API keep their existing fields.

Root rows are loaded in batches of 400, in the selection's recency order.
Lifecycle metadata uses a known root directly instead of looking it up twice for
every root. The tree query selects only the fields used to display child agents
and derive their status, including pending-save and deletion state. Filtering,
participation, linked identities, search, archived sessions, pins, filed older
sessions and focused descendants use the same selection rules as before.

List construction, including identity registration and PR receipt reads, runs in
Starlette's bounded worker pool. Folder listing uses the same pool. SQLite disk
and lock waits in these operations therefore do not stall the server event loop.
The PR cache and asynchronous refresh scheduling remain on the event loop and
recheck live GitHub access there. No session data is cached in browser storage.

## Reproduce

```sh
uv sync --frozen
uv run python scripts/session_list_demo.py --port 8846
```

Open `http://127.0.0.1:8846/demo/list-login`. This is a localhost-only demo of the
real frontend and API, seeded with 100 listed sessions, 60 agents and approximately
33 KiB saved answers. It does not call SSO, models, GitHub or other connected apps.
The panel shows actual response duration, byte size and SQL statement count.
Choose All sessions, reload, expand agents and open a session's action menu.

For a repeatable API comparison without injected latency:

```sh
uv run python scripts/session_list_demo.py --benchmark --output /tmp/after.json
uv run python scripts/session_list_demo.py --benchmark \
  --root /path/to/baseline-checkout --output /tmp/before.json
```

One local seven-request comparison against `fe80bdb` with All sessions:

| Measurement | Before | After |
| --- | ---: | ---: |
| Median API duration | 195.62 ms | 19.51 ms |
| SQL statements, including authentication | 322 | 23 |
| Response bytes | 3,478,865 | 89,365 |

These are local synthetic measurements, not production percentiles. The sample
contains no GitHub PR receipts; cached PR status lookups may add database work.
Browser timing additionally includes HTTP transport and other startup requests.
Production tracing is still needed to attribute the entire reported delay.

The regression suite checks the query/payload budgets, complete answers in the
full and detail APIs, batch order, permissions, nested states, search, pins,
folders, archives, focus and background PR refresh. It also holds a list database
read open and verifies another HTTP request completes before releasing it. The
query-budget and event-loop tests fail against the original implementation.

## PostgreSQL

These changes reduce work on either database. PostgreSQL is a separate runtime
migration that can improve concurrent writes and support multiple application
instances once ownership/recovery is made safe. It does not automatically remove
per-session queries or oversized HTTP responses. The current
[migration rehearsal](postgres-migration.md) copies data but does not switch the
application's runtime database.
