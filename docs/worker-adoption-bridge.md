# Preparing an unversioned worker fleet

The adoption bridge lets an explicitly authorized versioned replacement join
the compatibility contract of an unversioned installation. Initial source and
replacement processes must run the same application image. Changing workflow
code while enabling versioning is a separate compatibility problem.

This is a startup and registration protocol. It does not promote Temporal
versions, change HTTP routing, install application migrations, or authorize
retirement of unversioned workers.

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

The runtime therefore reports
`worker_adoption.unversioned_retirement_qualified=false`. An unversioned worker
cannot be sealed using an invented version-drainage receipt. The supported
versioned release path retains its existing drainage requirements.

Initial traffic promotion, unversioned-worker drain/recovery qualification,
provider resource provisioning, and schema-upgrade qualification remain
explicit operator gates. Preparing a binding does not make an existing
installation ready for a zero-downtime cutover.
