# Install Moyai with Docker

This package runs the Pydantic AI/Harness core introduced in PR #465. Bring a
PostgreSQL database, private S3-compatible storage, a LiteLLM gateway with a
Responses-capable model, and Modal credentials. Choose an external Temporal
service or the included persistent Temporal server.

Use an empty, dedicated application schema for a new installation. Migrating an
existing Moyai deployment still requires the procedure in
`docs/pydantic-deployment.md`; this initializer refuses to adopt populated schemas
without their original installation state.

Use Docker Compose 2.30 or newer for raw env-file support.

## First installation

Download `moyai-docker.tar.gz` from a Moyai GitHub release and extract it. The
bundle's `.env.example` already names the release image by immutable digest.
Until a release includes that bundle, use the checkout build instructions below.
From the extracted `moyai-docker` directory:

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your endpoints and credentials. Container-to-service addresses
must be reachable from Docker; `localhost` inside a container names that
container. The installation ID is 1–8 lowercase letters/digits and starts with a
letter. It determines separate application schemas, object prefixes, and queues.
Each installation needs its own ID and Compose project.

| Setting | Value |
| --- | --- |
| `MOYAI_DATABASE_URL` | PostgreSQL URL. The database role must own the installation schemas and have permission to prepare their tables/functions. |
| `OBJECT_STORAGE_BUCKET` | Existing private bucket; allow read and write for the configured prefixes. |
| `OBJECT_STORAGE_REGION` | Bucket region. |
| `OBJECT_STORAGE_ENDPOINT` | Empty for AWS S3; set for another compatible service. |
| `OBJECT_STORAGE_ACCESS_KEY_ID`, `OBJECT_STORAGE_SECRET_ACCESS_KEY` | Credentials, or leave empty to use the AWS workload credential chain. |
| `MOYAI_PUBLIC_URL` | The HTTPS origin your browser and workers can reach. |
| `LITELLM_API_BASE`, `LITELLM_API_KEY`, `AGENT_MODEL` | Gateway URL, key, and an enabled model that supports Responses. |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | A token pair for your Modal workspace. |
| `MODAL_APP_NAME` | A dedicated Modal application name for this installation. |
| `WORKSPACE_PASSWORD` | An initial administrator password of at least 16 characters, or leave empty to generate one. |

The app's `.env` uses Compose's raw env-file format. Enter values without shell
quotes. Do not commit this file or include it in support logs. Compose itself
interpolates the image, project, port, and bundled-database settings; use an
alphanumeric password for `MOYAI_TEMPORAL_DATABASE_PASSWORD`.

Place your existing HTTPS ingress in front of the bound proxy port and preserve
the incoming Host header. The default binding is `127.0.0.1:8787`. Set
`MOYAI_BIND_ADDRESS` to the appropriate private interface when ingress runs on a
different host. Keep the database, Temporal, and individual application services
private. Configure the ingress for streaming and WebSockets.

### Choose the topology

Set `COMPOSE_FILE=compose.yaml` for one application container with an embedded
worker and dispatcher. It uses the fresh native application's account and
conversation model.

Set `COMPOSE_FILE=compose.split.yaml` for the API, coordinator, broker, and worker
as separate containers. This mode prepares both canonical product and native
execution schemas and uses the product's identity, storage, and accounting
adapters. It starts the coordinator before the other roles and resumes execution
only after all four report the same release identity.

These modes have different identity/schema contracts. Changing `COMPOSE_FILE`
does not migrate an existing standalone installation into the split topology.
Use a new installation or an explicitly verified migration.

### Choose Temporal

For Temporal Cloud or an existing self-hosted server, keep
`MOYAI_TEMPORAL_MODE=external`. Configure `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, and the appropriate TLS/API-key settings. The namespace
must already exist. Moyai does not request cloud-administration credentials.

For the included server, set `MOYAI_TEMPORAL_MODE=bundled` and append
`:compose.temporal.yaml` to `COMPOSE_FILE`. Supply a dedicated
`MOYAI_TEMPORAL_DATABASE_PASSWORD`. Leave `TEMPORAL_NAMESPACE` empty to use an
installation-specific name, or choose one. The package starts a separate
PostgreSQL service for Temporal persistence/visibility, applies the pinned
Temporal schemas, and registers the namespace. Its database volume survives
container restarts. Temporal remains internal to the Compose network.

### Start

```sh
docker compose up -d
docker compose ps -a
docker compose logs setup
```

The setup service checks PostgreSQL, reads and writes a small encrypted storage
probe, checks the Temporal namespace, lists the selected gateway model, and
authenticates the Modal client. It then prepares application schemas. These
checks do not execute a model or start a computer.

Retrieve the generated initial password only in your private terminal:

```sh
docker compose run --rm --no-deps setup \
  /app/native/.venv/bin/python -m moyai.deployment password
```

Open `MOYAI_PUBLIC_URL`, sign in, and ask Moyai to create a small file in its
computer and save it for download. Verify the file, restart the app, and confirm
you can reopen the session and download it again. The first task may build the
Modal computer image. A successful setup check alone does not establish model,
computer, or tool compatibility.

Run connection and schema checks again with:

```sh
docker compose run --rm --no-deps setup \
  /app/native/.venv/bin/python -m moyai.deployment check
```

## State and recovery

The `moyai-state` volume contains private `installation.json` with stable signing
and encryption keys, the data-destination fingerprint, and installation/upgrade
receipts. Back it up together with PostgreSQL and the referenced object population.
For bundled Temporal, retain a consistent backup of its persistence database too.
Do not remove volumes to resolve startup failures.

An ordinary restart retains the same keys and verifies the prepared schema.
Changing the database destination, topology, bucket/prefixes, namespace, or
public origin requires an explicit migration. Credentials may be rotated while
retaining those destinations and keys.

If initialization failed after writing its receipt, inspect the database and
service error first. Keep runtime containers stopped, correct the underlying
problem, then explicitly resume preparation with the original image:

```sh
docker compose run --rm --no-deps setup \
  /app/native/.venv/bin/python -m moyai.deployment recover-setup
docker compose up -d
```

This command retains the original keys and uses the same migration guards.
Missing installation state alongside populated schemas requires restoring the
original state backup; the package does not generate replacement keys for that
data.

## Maintenance upgrades

The package supports planned maintenance upgrades within the Pydantic
core. Download and inspect the candidate release before the maintenance window.
Back up application data, objects, `.env`, and installation state. Keep the old image
digest and current bundle for recovery.

```sh
sh upgrade.sh ghcr.io/berriai/moyai@sha256:THE_CANDIDATE_DIGEST
```

For split mode, the helper requests a generation-bound drain from the live worker, waits for
safe saved boundaries, stops ingress and the four old owners, prepares schemas
with the new image, and starts matching new roles. It resumes only after their
health identities match. Existing queued/parked native work remains preserved.
The helper updates `MOYAI_IMAGE` in `.env` after successful schema preparation.
It does not switch execution cores, automatically roll back, or bypass unknown
external effects.

If any step fails, execution stays paused or stopped. Inspect the recorded
upgrade phase and database migration state before continuing; do not rerun a
possibly applied migration or resume command blindly. Use the installed image's
`status` command while the schemas remain readable. Restore a consistent backup
or complete a reviewed repair for a partial migration. A changed schema may
prevent rollback to the old image.

For standalone mode, finish all work first. The same helper stops ingress and
the application, verifies that the application released its runtime lock, and
refuses to migrate while unfinished turns or uncertain effects/requests remain.
If that check fails before migration, restart the old image and settle the work.
Standalone replicas are unsupported; the shared runtime lock prevents a second
container in the installation from starting.

Bundled Temporal version/database upgrades are a separate maintenance
operation; an application image upgrade does not update the pinned Temporal
services.

## Build from a checkout

From the repository root:

```sh
docker build --build-arg MOYAI_BUILD_SHA="$(git rev-parse HEAD)" -t moyai-local .
```

Set `MOYAI_IMAGE=moyai-local` in a private copy of this bundle's `.env`. Release
images contain the actual Git revision; do not override it to bypass admission.
The Docker Node stage runs `npm ci` and `npm run build:native`. The runtime image
contains generated assets and the separately locked Python environments, without
Node or `node_modules`.

The release workflow publishes AMD64/ARM64 images and attaches a digest-pinned
installation bundle when a GitHub release is published. That workflow does not
deploy Moyai or migrate a customer's data. Helm/EKS and AWS provisioning remain
follow-up work.
