# Conversation event polling

Every open conversation checks for new activity every 0.5 seconds. Previously a
durable idle root conversation performed seven application SELECT statements and
seven `Store.connect()` checkouts per poll: two decorated run reads (each loading
unused swarm metadata), events, root ancestry, and Slack binding state.

The poll now reads only status fields and events in one read-only snapshot. Direct
root sessions need no ancestry query; child sessions resolve their root on the same
connection. Durable activity uses the status already read, while local activity
still checks live job ownership. Slack mirroring/policy reads happen after the
snapshot releases its connection, retaining fresh pause and credential checks
without nested pool acquisition. Stream authentication also runs off the event loop.

The 0.5-second interval, event cursor, batch size, terminal settlement, deletion
notifications, and initial/old conversation history API remain unchanged.

## Local measurement

`scripts/event_polling_probe.py` starts the actual app with its Temporal-backed
manager and opens a real local HTTP event stream. It disables background workers
and uses isolated synthetic data, with no Temporal or provider connection. After
the initial response it counts six idle polls, then commits an event/status update
through another connection and checks both live delivery and reconnection cursors.

| Backend | Version | SELECTs per idle poll | Store checkouts per poll | Six polls | Fresh update observed |
| --- | --- | ---: | ---: | ---: | ---: |
| SQLite | Main `c87d4e9` | 7 | 7 | 3.054 s | 504.1 ms |
| SQLite | This change | 3 | 2 | 3.022 s | 499.7 ms |
| PostgreSQL | Main `c87d4e9` | 7 | 7 | 3.033 s | 503.9 ms |
| PostgreSQL | This change | 3 | 2 | 3.018 s | 501.6 ms |

These counts cover application SELECTs, excluding transaction control and database
driver/runtime guard queries. Checkouts are calls to `Store.connect()`, not physical
network connections. Extra Slack installation/policy reads still occur for bound
Slack conversations, and child sessions require one additional ancestry SELECT.
Timing is illustrative local evidence, not a production navigation or capacity
benchmark. The reduction is in recurring database work; polling cadence is unchanged.
The final probe was also run with unrelated authentication and worker-role shell
settings present; explicit defaults and an isolated initial import keep them out
of the demo.

Run from the repository root:

```sh
uv run --frozen python -m scripts.event_polling_probe --output /tmp/event-polling.json
uv run --frozen python -m scripts.event_polling_probe --database-url postgresql:///postgres --output /tmp/event-polling-postgres.json
```

The PostgreSQL command creates and removes a unique `moyai_poll_probe_*` schema.
Use a disposable local database with schema-creation privileges.

## Regression coverage

```sh
uv run --frozen pytest -q tests/test_event_polling.py tests/test_conversation_responsiveness.py tests/test_sessions.py tests/test_durable.py
MOYAI_TEST_POSTGRES_URL=postgresql:///postgres uv run --frozen pytest --postgres-backend -q tests/test_event_polling.py tests/test_conversation_responsiveness.py tests/test_sessions.py tests/test_durable.py
```

The focused tests cover multi-batch replay and final results, a concurrent writer
between the status and event reads, local/durable ownership, deletion and nested
root errors, Slack policy changes, authentication, and a one-slot PostgreSQL pool.
SQLite skips only the two pool-exhaustion cases. The PostgreSQL CI workflow runs the
focused suite on both backends.

Full-transcript transfer and further idle-poll coalescing remain separate targets.
Reducing the poll frequency requires a cross-process wake mechanism so updates from
another API/worker do not wait longer. History pagination must retain old-history
access and coordinate the initial message snapshot with event replay.
