# Preparing an unversioned worker fleet

The adoption bridge lets an explicitly authorized versioned replacement join
the compatibility contract of an unversioned installation. Initial source and
replacement processes must run the same application image. Changing workflow
code while enabling versioning is a separate compatibility problem.

Startup and registration do not promote Temporal versions, change HTTP routing,
install application migrations, or authorize retirement on their own.

## Runtime configuration

| Configuration | Contract and admission |
| --- | --- |
| Deployment/build unset; bridge unset | Existing unversioned behavior. An enabled fleet registry additionally requires an approved unversioned source binding. |
| Deployment/build set; bridge unset | Existing versioned behavior. The deployment name remains part of the compatibility contract. |
| Deployment/build set; `MOYAI_WORKER_ADOPTION_BRIDGE=true` | Retains the unversioned contract shape, and requires an enabled registry plus a matching durable adoption binding before role work starts. |

The bridge flag cannot disable required admission. It is restricted to
distributed existing-product roles. Namespace, queues, storage/key contracts,
role policies, and schemas must still match. No stored fingerprint is rewritten.

The operator creates an immutable `moyai-worker-adoption-v1` binding in the
optional `worker_adoption` table of the source-owned fleet registry. It records
the source and target generations, exact source image commit, target
deployment/build, canonical policy, and a digest of the reviewed configuration.
Runtime startup never creates or repairs this table.

While prepared, a versioned replacement is admitted only for the exact target
generation/build and the source image. Another deployment, image, generation,
or configuration fails before polling or serving requests. Source registration
uses the original canonical owner lifetime and the existing startup/seal gate.
The early permit check reuses the read-only schema-verification connection.
Ongoing admission reads use the existing pooled, offloaded connection path;
the bridge creates no additional pool.

## Preparation and activation

Metadata preparation installs a disabled fleet scope first. This gives
concurrent startups a visible row on which a later registration operation can
synchronize. Registering the source requires a fresh inventory under the
exclusive scope-row lock; an unknown owner aborts the transaction.

Preparation can be cancelled while the scope remains disabled, no member has
registered, and no candidate service is bound. A cancelled request cannot be
revived implicitly. A new preparation uses a new request identity.

The `source_registered` state permits the approved replacement to warm up.
It does not prove traffic or workload handoff. The `active` state additionally
requires source retirement and target acceptance; qualified operator tooling
must separately establish the routing and workload evidence for that transition.

## Remaining cutover gates

Unversioned histories can move to the current version at their next workflow
task. Already-running activities can remain on an older worker. A one-time
history replay or an empty visibility query cannot establish that every such
activity has settled.

The runtime reports `worker_checkpoint_retirement=moyai-adoption-checkpoint-v1`
for a separate, one-time checkpoint retirement primitive:

1. Both generations use the exact same image and authorized binding. A target
   worker must have a live registered owner; operator tooling must also verify
   every required Temporal poller.
2. Request the native maintenance pause and wait for saved checkpoints, settled
   effects, and settled model requests. HTTP intake can continue queuing work.
3. Under that pause, promote the approved Temporal version and move HTTP traffic
   through the verified stable ingress.
4. Seal the old worker with the exact pause generation, request key and
   fingerprint, target binding, and routing observations. The seal takes a
   shared lock on the native pause row, ordering it against resume.
5. Before stopping polling, the old worker independently verifies the same pause
   and rechecks unsafe turns, effects, and requests. Its completion receipt is
   written only after graceful worker shutdown.
6. Retire the exact old owner. Resume is rejected while any captured, sealed
   unversioned owner remains live, including after it reports drained. The
   replacement can then resume from the durable checkpoints.

No connection is held across worker shutdown, so this protocol also works with
one connection per pool. Restarting a sealed service cannot readmit it. A failed
checkpoint check or shutdown leaves the process unqualified for retirement;
operator tooling must inspect the saved intent and live state.

This pauses task execution during initial adoption. It does not claim continuous
execution, and does not by itself qualify provider provisioning, traffic
switching, or recovery. Consequently
`worker_adoption.unversioned_retirement_qualified` remains `false`. An invented
Temporal version-drainage receipt still cannot seal an unversioned worker. Normal
versioned releases retain their existing drainage requirements.

Initial traffic promotion, unversioned-worker drain/recovery qualification,
provider resource provisioning, and schema-upgrade qualification remain
explicit operator gates. Preparing a binding does not make an existing
installation ready for a zero-downtime cutover.
