# Compatible execution worker releases

A verified-schema, separate-broker cluster can admit different execution worker
builds under a source-controlled compatibility contract. Old and new workers
consume the same Temporal queue. PostgreSQL session leases still permit only one
activity to advance a session at a time. This removes the exact-build barrier for
compatible **worker-only** replacements; users can keep submitting messages.

The [rolling deployment action](rolling-deploy.md) combines this worker handoff
with broker overlap, coordinator leadership transfer and API replacement. This
page describes the worker stage. The default standalone and auto-schema modes
retain their existing ownership rules. Initial production activation is separate
from a compatible rolling release; merging code alone does not activate it.

## Activation and compatibility

The cluster must use PostgreSQL, Temporal, verified shared object storage,
`MOYAI_SCHEMA_MODE=verify`, and `MOYAI_SEPARATE_BROKER=true`. Schema revision 7
adds the background role contracts, broker ownership and coordinator hook inbox; migrate offline with **all runtime owners stopped**
using the existing [migration procedure](schema-migrations.md). Start the matching
coordinator first so it publishes the contract. Migration alone does not publish
worker admission. This initial transition is not an overlapping update.

`WORKER_PROTOCOL_REVISION` in `app/runtime_compatibility.py` covers durable session
state, sandbox launch journals and capabilities, Temporal workflow histories and
activity arguments/results, and the worker's writes consumed by other roles. The
fingerprint also includes all background and API protocols, exact schema revision and every shared
runtime setting except build SHA. The coordinator publishes all enabled role
contracts atomically with its exact cluster policy after component setup succeeds.
Workers never publish a policy. Auto-schema and co-located-broker clusters retain
exact-build worker admission.

Every worker transaction checks the ownership connection, cluster policy and
worker contract. Writes lock the contract row until commit; revocation waits for
already admitted writes and rejects later transactions. Lease tokens are still
checked on every scoped write. Losing a contract cannot authorize a new lease.
The manager's database pulse stops Temporal consumption after a fence is lost;
this does not reverse an external request already issued.

Equal revisions declare compatibility; they do not prove arbitrary changes safe.
For each real release, review both directions and test the actual old/candidate
pair. Candidate workers must understand old journals and pending workflows. Old
workers must understand candidate writes and workflow histories during overlap
and rollback. Keep provider, harness, feature and security settings aligned too;
the fingerprint is not an exhaustive settings validator. Replay saved workflow
histories. Bump the worker protocol for incompatible changes; bump the schema
revision for schema changes. Use maintenance when compatibility is uncertain.

## Replacement and rollback semantics

1. Keep the coordinator, broker, API, shared configuration and capacity fixed.
   Budget database connections for both worker generations.
2. Start the reviewed candidate with its actual build SHA and a fresh ephemeral
   directory. Worker startup waits for Temporal and database readiness before
   binding TCP, bounded by `TEMPORAL_STARTUP_TIMEOUT_SECONDS`. Check `/health`
   again during overlap; dependency loss returns 503 but does not close the port.
3. Once the candidate is healthy and demonstrably executing work, stop the old
   worker gracefully. Temporal stops its polling, allows the existing one-second
   activity grace period and retries detached activities on surviving workers.
   The session journal and lease retain ownership; a live sandbox supervisor
   continues running. Do not drain the entire session queue or cancel user turns.
4. Verify the same model stream, session capability and journal survive; new
   submissions complete; each user message gets one answer; model calls and
   external writes are not replayed. Expect bounded observation delay from
   activity retry, not a guarantee of zero scheduling latency.
5. For a reviewed compatible rollback, start the previous worker before stopping
   the candidate and perform the same checks against candidate-written state.
   Keep both builds' actual identities. Do not mutate the coordinator policy.

Abrupt process death additionally waits for the existing 30-second session lease
and Temporal heartbeat/retry. Long database writes and provider uncertainty can
extend recovery. A failed/ambiguous external action retains its existing durable
handling; compatibility does not permit replaying it. Broker and coordinator replacement use their separate overlap protocols.
Incompatible schema changes still require a maintenance transition.

## Reproduce locally

Set `MOYAI_TEST_POSTGRES_URL` to a disposable PostgreSQL database:

```sh
uv run --frozen --python 3.13 pytest -q tests/test_worker_compatibility.py tests/test_worker_handoff.py
uv run --frozen --python 3.13 python -m scripts.worker_release_probe --all-roles --output /tmp/moyai-rolling-release
```

The process probe uses real PostgreSQL, Temporal and separate coordinator, API,
broker and worker processes. It copies the source, changes the candidate's demo
response text, derives distinct build IDs, holds an old activity under its session
lease, submits HTTP work continuously, and verifies upgrade and rollback produce
one answer per submission. An incompatible source protocol is rejected before
readiness. With `--all-roles`, the probe then starts a replacement broker while the first
stream is open, checks global capacity, drains the old stream through SIGTERM,
and sends another inference through the replacement. A coordinator standby
accepts signed synthetic hooks before becoming leader; the replacement API
accepts messages before the old API exits. All roles finish on the candidate
source. Omit `--all-roles` to isolate just the worker stage. Saved Temporal histories are replayed afterwards.

The integration test separately exercises the real durable sandbox controller
with a deterministic Modal boundary: the replacement observes the same live
sandbox, launch journal and capability, then finishes a queued follow-up. No cloud
machine, paid model or S3 transfer is used. These are local protocol proofs, not
production routing/performance measurements or a certification of future releases.
