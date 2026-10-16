# Simplified Docker deployment after PR #465

The Docker package implements the first three milestones from the original
simplified-deployment plan. It targets the Pydantic AI/Harness execution core
merged in #465. Use [the installation guide](../deploy/docker/README.md) for the
configuration and commands.

A customer supplies PostgreSQL, private S3-compatible storage, an external
Temporal namespace or the bundled-server choice, a LiteLLM gateway/model, and
Modal credentials. One versioned image runs standalone or as the API,
coordinator, broker, and execution worker.

## Implemented

| Work | Implementation |
| --- | --- |
| Build the UI from source | A pinned Node build stage runs `npm ci` and `npm run build:native`. Python source builds use the same commands. Wheels and source distributions include the compiled assets; Git excludes the generated directory. |
| Build UI in CI and releases | Frontend CI builds the UI and checks it is untracked. Python-package CI installs Node for the build hook. Docker smoke tests, image releases, and Render use the Dockerfile's Node build stage. |
| Distribute installation artifacts | The release workflow builds AMD64/ARM64 images and attaches a bundle containing the immutable image digest, Compose files, configuration example, routing, and instructions. Publication occurs when an operator publishes a GitHub release. |
| Configure an installation | The setup command validates required inputs and persists signing/encryption keys in a private installation volume. It rejects changed data destinations and adoption of populated schemas without the original state. |
| Guide first installation | `moyai.sh configure` uses the image's Python runtime to generate private configuration and select the topology/Temporal Compose files. An environment-credential mode avoids writing provider secrets. Existing configuration is never overwritten. |
| Start and report readiness | `moyai.sh up` validates before replacing services, waits for service health and the split activation job, and reports HTTP readiness separately from real model execution. Read-only status checks remain available during maintenance. |
| Prepare schemas | One-shot setup reuses the canonical and native migration code, including separate Harness receipts. Application startup verifies schemas. A PostgreSQL mutex prevents simultaneous installers from preparing the same namespace. |
| Choose Temporal | External mode checks a supplied namespace. Bundled mode uses a pinned server, durable PostgreSQL persistence/visibility databases, and explicit namespace initialization. |
| Choose topology | Standalone includes its worker and dispatcher. Split mode starts four roles with shared configuration and routes ordinary requests, `/hooks/*`, and `/broker/*` to their owners. |
| Activate execution | Split activation checks each role's release identity and the public broker's exact owner. Broker routing starts first; browser/webhook ingress opens only after resume is confirmed. Initial activation requires an untouched, drained bootstrap generation and retains its exact resume command for recovery after interruption. Later maintenance remains an explicit operation. |
| Check dependencies | Diagnostics test PostgreSQL, a storage round trip, Temporal namespace access, the gateway model catalog, and Modal authentication. They report that real model/computer execution still needs a task check. |
| Handle shutdown | The package shares its 240-second worker allowance across the four sequential Temporal pollers, with 270 seconds for HTTP shutdown and 300 seconds for Docker. Existing non-package defaults remain intact. |
| Upgrade | Split upgrades preserve exact drain/resume commands and reconcile native receipts after interruption. They reject changed maintenance generations or workers. Standalone upgrades require stopped runtime ownership and no unfinished work or uncertain effects/requests. Failed or ambiguous migrations remain stopped for inspection. |

## Changes from the earlier plan

- The execution engine is now Pydantic AI/Harness. The original Deep Agents
  assumptions no longer define this package.
- Standalone and split modes use different identity/schema contracts. Switching
  a Compose filename does not migrate an installation between them.
- The package currently supports Modal computers. The older repository's other
  sandbox adapters do not establish native-runtime support.
- Native releases use maintenance. Compatible rolling upgrades remain subject
  to separate qualification; this package does not enable them.
- PostgreSQL and object storage remain customer-provided. Bundled Temporal has
  its own persistent database service.

## Release qualification

Use the actual baseline and candidate applications, isolated data, a real
configured model/gateway, and real Modal computers. Verify browser sign-in,
creation and download of a file, persistence after restart, role routing,
maintenance replacement, and reopening the saved session. Keep actual browser
recordings and screenshots with the PR handoff.

The automated checks cover clean frontend/package builds, source-distribution
wheel reconstruction without Node, private bundle contents, configuration/key
retention, concurrent setup/runtime exclusion, PostgreSQL URI handling, and
upgrade refusal/recovery boundaries. Fixture tests supplement the live
application proof.

## Follow-up work

- Helm/EKS packaging using the same configuration and ownership contracts.
- Optional AWS provisioning for RDS, S3, IAM, and networking.
- Qualification of additional native sandbox providers.
- Native rolling deployment support after its migration/drain protocols are
  qualified.
- Broader operational qualification for managed Temporal, custom ingress
  configurations, and larger worker counts.
