# Broker replacement while requests continue

Verified PostgreSQL brokers can overlap after the offline schema migration and
compatible runtime policy have been activated. Legacy `auto` schema startup still
requires one broker. The source protocol, schema and shared configuration must
match the coordinator's admitted contract; sharing a schema is not sufficient.

Each active broker holds a shared inference membership lock (`726944`). A model
attempt and its ownership receipt commit together. Durable context compaction
uses the same protocol. Startup and periodic recovery interrupt only work whose
exact PostgreSQL ownership session has ended, identified by backend PID **and**
backend start time. Live work is never considered abandoned based on age. An
interrupted model attempt is recorded, not resubmitted. Legacy ambiguous tool
writes become `uncertain`; the deployment path does not replay external actions.

Foreground model requests, native streams, credential inference and maintenance
share the same configured model limit across generations. One extra PostgreSQL
session per broker holds individual slot locks (`726947`), independently of its
runtime ownership connection. Losing runtime ownership therefore does not free
capacity while the still-running process closes its provider transport. The
watchdog rejects new requests and cancels owned routes on loss of ownership.
No per-request database connection or expiring capacity lease is required.
Foreground calls can preempt that process's tool-free maintenance; maintenance
running on another broker retains its slot until it finishes or drains.

Memory review, skill learning and spend recovery use one separate leader lock (`726946`) on the
runtime ownership session. A new broker can become ready and accept foreground
requests while the previous generation retains this maintenance leadership. The
old generation closes maintenance before releasing leadership. Context jobs and
model requests retain their individual ownership throughout this handoff.

On Render, broker shutdown allows existing HTTP requests up to **270 seconds**,
with a required platform shutdown allowance of **300 seconds**. The replacement
must become ready before the platform stops the old instance. Requests longer
than the drain allowance can still be interrupted. This is a bounded graceful
deployment guarantee, not a promise that an unlimited stream survives termination
or a provider/network failure. A stopped process never automatically replays a
possibly billed model request or an external tool write.

`tests/test_broker_overlap.py` checks real PostgreSQL ownership, maintenance
transfer, unchanged capacity at 32 and 128, a separate-process crash, and an old
native SSE stream completing while a replacement starts and serves new inference.
Temporal and the model provider are deterministic test boundaries; these tests do
not constitute a production Render routing rehearsal.
