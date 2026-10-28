# Install Moyai with Docker

This package runs the Pydantic AI/Harness core introduced in PR #465. Bring a
PostgreSQL database, private S3-compatible storage, a LiteLLM gateway with a
Responses-capable model, and Modal credentials. Choose an external Temporal
service or the included persistent Temporal server.

Use an empty, dedicated application schema for a new installation. Migrating an
existing Moyai deployment still requires the procedure in
`docs/pydantic-deployment.md`; this initializer refuses to adopt populated schemas
without their original installation state.

Use Docker Compose 2.30 or newer for raw env-file support. The helper uses the
image's Python runtime; you do not need Python or Node installed on the host.
Run it on the machine hosting Docker.

## First installation

Download `moyai-docker.tar.gz` from a Moyai GitHub release and extract it. The
bundle's `.env.example` already names the release image by immutable digest.
Until a release includes that bundle, use the checkout build instructions below.
Export the settings below, then run one command from the extracted
`moyai-docker` directory:

```sh
sh moyai.sh up
```

On first startup, `up` reads the exported environment, validates it, chooses the
Compose files, and starts setup and the application. No wizard or separate
configuration command is required. PostgreSQL, storage, model and provider
credentials stay in the invoking environment. Generated non-provider
configuration is saved in a private `.env`; when bundling Temporal, its generated
database password is also saved there. Keep provider credentials exported for
future starts and upgrades.

For Temporal Cloud or your own server, supply `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, `TEMPORAL_TLS` and any required `TEMPORAL_API_KEY`.
External Temporal is the unattended default. To run the included Temporal
server instead, set `MOYAI_TEMPORAL_MODE=bundled`; setup prepares its persistent
database and namespace. No separately configured Temporal service or database
password is required.

Set `MOYAI_INSTALL_TOPOLOGY=split` for all four application roles, or leave it
unset for standalone. The same command handles either choice.

The application generates and retains its own missing signing/encryption keys
during setup. Subsequent starts retain the saved configuration and data
destinations. To change them, edit the configuration deliberately and follow
the migration/upgrade instructions below.

If you prefer prompts, run `sh moyai.sh configure` first. The optional wizard
writes endpoints and supplied credentials to `.env` with mode 0600 and refuses
to overwrite existing configuration.

For manual configuration, copy `.env.example` to `.env`, protect it with
`chmod 600 .env`, and fill in the settings below. Use the helper to start either
generated or manually prepared configuration.

Container-to-service addresses
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
| `MOYAI_PUBLIC_URL` | The HTTPS URL your browser and workers can reach, optionally including `/moyai`. |
| `LITELLM_API_BASE`, `LITELLM_API_KEY`, `AGENT_MODEL` | Gateway URL, key, and an enabled model that supports Responses. |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | A token pair for your Modal workspace. |
| `MODAL_APP_NAME` | A dedicated Modal application name for this installation. |
| `MOYAI_MODAL_RUNTIME` | `vm` (default) for a full Linux kernel, or explicit `gvisor`. Use the same value in every service; see [runtime selection](../../docs/modal-runtime.md). |
| `WORKSPACE_PASSWORD` | An initial administrator password of at least 16 characters, or leave empty to generate one. |

The app's `.env` uses Compose's raw env-file format. Enter values without adding
shell quotes. The helper reads only the image/project/port settings needed for
Compose interpolation; credentials remain literal raw values, including dollars
and quotes. Do not source `.env` as a shell script, commit it, or include it in
support logs. Use letters, digits, underscores or hyphens for
`MOYAI_TEMPORAL_DATABASE_PASSWORD`.

### Example: external services

Provide these through your secret manager or private shell environment:

```sh
export MOYAI_PUBLIC_URL=https://moyai.example.com
export MOYAI_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
export OBJECT_STORAGE_BUCKET=your-private-bucket
export OBJECT_STORAGE_REGION=us-east-1
# Export storage credentials, or use your AWS workload credentials.
export TEMPORAL_ADDRESS=your-namespace.tmprl.cloud:7233
export TEMPORAL_NAMESPACE=your-namespace
export TEMPORAL_TLS=true
export TEMPORAL_API_KEY=YOUR_TEMPORAL_KEY
export LITELLM_API_BASE=https://your-gateway.example.com/v1
export LITELLM_API_KEY=YOUR_MODEL_KEY
export AGENT_MODEL=YOUR_RESPONSES_MODEL
export MODAL_TOKEN_ID=YOUR_MODAL_ID
export MODAL_TOKEN_SECRET=YOUR_MODAL_SECRET
export MOYAI_INSTALL_TOPOLOGY=split
sh moyai.sh up
```

For bundled Temporal, omit the external Temporal connection settings and export
`MOYAI_TEMPORAL_MODE=bundled` before the same `up` command. For S3-compatible
storage, export `OBJECT_STORAGE_ENDPOINT` too.

### Optional separate configuration

To generate and inspect configuration before startup:

```sh
sh moyai.sh configure --from-env --secrets-from-env --topology split --temporal bundled
sh moyai.sh up
```

This writes a `compose.environment.json` override containing credential names
without values. Keep the credentials exported whenever starting, recreating, or
upgrading containers. If you export `MOYAI_TEMPORAL_DATABASE_PASSWORD`, it also
stays in the invoking environment; otherwise the generated internal database
password is retained in private `.env`.
Database URLs and initial signing/encryption/password values are also kept in
the environment. Missing application keys are still generated once and retained
in the private installation volume.

Omit `--secrets-from-env` to store supplied credentials in private `.env` instead.
Automatic first startup uses the environment-credential mode.

You can append a custom Compose override to `COMPOSE_FILE` in `.env`, for example
to mount a private certificate bundle. The helper uses this directory's
configured project, image and Compose files.

Place your existing HTTPS ingress in front of the bound proxy port and preserve
the incoming Host header. The default binding is `127.0.0.1:8787`. Set
`MOYAI_BIND_ADDRESS` to the appropriate private interface when ingress runs on a
different host. Keep the database, Temporal, and individual application services
private. Configure the ingress for streaming and WebSockets.
Compose restarts the proxy when it replaces an upstream application container,
so Nginx resolves the new container addresses.

### Serve under a path

Set `MOYAI_PUBLIC_URL=https://gateway.example.com/moyai` before configuration.
The helper derives `MOYAI_BASE_PATH=/moyai`; for manually prepared `.env` files,
set both consistently. Forward the path unchanged to the proxy. The same image
serves assets, APIs, streams and callbacks below that prefix. Register enabled
OAuth callbacks with the prefix. Keep Cloudflare provider endpoints such as
`/cdn-cgi/access/logout` at the hostname root. Changing an existing installation's
public URL/path requires an explicit migration.

### Choose the topology

Before the first `sh moyai.sh up`, leave `MOYAI_INSTALL_TOPOLOGY` unset or export
`MOYAI_INSTALL_TOPOLOGY=standalone` for one application container with an embedded
worker and dispatcher. The helper selects `compose.yaml`. This mode uses the
fresh native application's account and conversation model.

Export `MOYAI_INSTALL_TOPOLOGY=split` for the API, coordinator, broker, and worker
as separate containers. The helper selects `compose.split.yaml`. This mode
prepares both canonical product and native execution schemas and uses the
product's identity, storage, and accounting adapters. It starts the coordinator
before the other roles and resumes execution
only after all four report the same release identity.
The proxy exposes broker routing before activation, while browser and webhook
requests receive maintenance HTTP 503. Activation checks the public
`/broker/health` route against the internal broker's build and owner before
resuming work and opening ingress. `/health` stays available for your load
balancer during this check. If Cloudflare Access protects the public origin,
configure `CLOUDFLARE_ACCESS_CLIENT_ID` and `CLOUDFLARE_ACCESS_CLIENT_SECRET`
with permission to reach the broker routes.

These modes have different identity/schema contracts. Changing the topology
variable or `COMPOSE_FILE` does not migrate an existing standalone installation
into the split topology. Use a new installation or an explicitly verified
migration.

### Choose Temporal

For Temporal Cloud or an existing self-hosted server, keep
`MOYAI_TEMPORAL_MODE=external`. Configure `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, and the appropriate TLS/API-key settings. The namespace
must already exist. Moyai does not request cloud-administration credentials.

For the included server, export `MOYAI_TEMPORAL_MODE=bundled` before the first
`sh moyai.sh up`. The helper adds `compose.temporal.yaml` automatically and
generates a database password, or uses `MOYAI_TEMPORAL_DATABASE_PASSWORD` if you
export one. Leave `TEMPORAL_NAMESPACE` empty to use an
installation-specific name, or choose one. The package starts a separate
PostgreSQL service for Temporal persistence/visibility, applies the pinned
Temporal schemas, and registers the namespace. Its database volume survives
container restarts. Temporal remains internal to the Compose network.

If you prepare `.env` manually instead of using automatic configuration, select
the topology's Compose file in `COMPOSE_FILE`, append `:compose.temporal.yaml`
for bundled Temporal, and supply its database password yourself.

### Start

```sh
sh moyai.sh up
docker compose ps -a
docker compose logs setup
```

The helper first validates configuration and any existing installation's image
and data destination. It then waits for service health and, in split mode, the
activation job's successful completion. It does not treat a running activation
job as completed. A final HTTP/readiness check reports the application URL and
explicitly states that model execution has not been checked.

Use `sh moyai.sh up --timeout 600` to set the timeout, in seconds, for each
readiness wait phase. A timeout leaves services and volumes in place for
inspection. `sh moyai.sh status` checks HTTP/activation readiness without waiting
behind a maintenance command's file lock.

The setup service checks PostgreSQL, reads and writes a small encrypted storage
probe, checks the Temporal namespace, lists the selected gateway model, and
authenticates the Modal client. It then prepares application schemas. These
checks do not execute a model or start a computer.

Retrieve the generated initial password only in your private terminal:

```sh
sh moyai.sh password
```

Open `MOYAI_PUBLIC_URL`, sign in, and ask Moyai to create a small file in its
computer and save it for download. Verify the file, restart the app, and confirm
you can reopen the session and download it again. The first task may build the
Modal computer image. A successful setup check alone does not establish model,
computer, or tool compatibility.

Run connection and schema checks again with:

```sh
sh moyai.sh check
```

Use `sh moyai.sh validate` for offline configuration validation only. A retried
initial activation reconciles its original native resume receipt, so a lost
acknowledgement does not cause another resume or leave a confirmed activation
permanently pending.

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
sh moyai.sh recover-setup
sh moyai.sh up
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
sh moyai.sh upgrade ghcr.io/berriai/moyai@sha256:THE_CANDIDATE_DIGEST
```

For split mode, the helper requests a generation-bound drain from the live worker, waits for
safe saved boundaries, stops ingress and the four old owners, prepares schemas
with the new image, and starts matching new roles. It resumes only after their
health identities match and the public broker route reaches the new broker.
Browser/webhook ingress opens only after resume is confirmed. A separate
`moyai-ingress` volume shares a nonsecret readiness marker with Nginx; the proxy
never mounts the private installation state. Existing queued/parked native work remains preserved.
The helper updates `MOYAI_IMAGE` in `.env` after successful schema preparation.
It does not switch execution cores, automatically roll back, or bypass unknown
external effects.

If the controller fails, its last native operation may already have committed.
In particular, a lost resume acknowledgement can leave execution running while
browser/webhook ingress remains closed. Inspect the recorded upgrade phase and
database migration state before continuing; do not rerun a
possibly applied migration or resume command blindly. Use the installed image's
`status` command while the schemas remain readable. Restore a consistent backup
or complete a reviewed repair for a partial migration. A changed schema may
prevent rollback to the old image.

If `begin-upgrade` times out or its controller exits while the original roles
are still healthy, rerun the helper's `upgrade` command with the same candidate digest. The saved
drain command retains its original worker, generation, and request key. Recovery
checks the native receipt before sending anything: an accepted pause is observed
without requesting another generation, and an uncommitted request can be retried
only against the same worker and generation.

If the candidate controller exits during `resume`, run:

```sh
sh moyai.sh resume
```

This also checks the saved command against the native receipt. A confirmed
resume opens ingress without repeating the operation. An unconfirmed or still
paused result keeps the upgrade pending. Changed generations/workers and older
interrupted records without an exact command require inspection; recovery does
not invent their original intent. These retry rules do not retry an uncertain
schema migration.

`sh moyai.sh maintenance-status` reads the native maintenance generation and
counts. `sh moyai.sh pause` requests a coordinated drain. These commands, along
with `resume`, apply to split installations.

Use the candidate bundle's Compose, proxy configuration, and upgrade helper
together. Starting the proxy with `--no-deps` during replacement avoids rerunning
setup and activation jobs after execution has resumed. If public broker
verification fails after schema preparation, fix the route and use the candidate
image's `resume` command; it rechecks routing before opening ingress.

For standalone mode, finish all work first. The same helper stops ingress and
the application, verifies that the application released its runtime lock, and
refuses to migrate while unfinished turns or uncertain effects/requests remain.
If that check fails before migration, restart the old image and settle the work.
Standalone replicas are unsupported; the shared runtime lock prevents a second
container in the installation from starting.

The execution worker contains four Temporal pollers. Their sequential shutdown
shares a total 240-second grace allowance; HTTP shutdown has 270 seconds and
Docker allows 300 seconds. The API/coordinator/broker/worker remain four
application roles using the same image.

Bundled Temporal version/database upgrades are a separate maintenance
operation; an application image upgrade does not update the pinned Temporal
services.

## Build from a checkout

From the repository root:

```sh
docker build --build-arg MOYAI_BUILD_SHA="$(git rev-parse HEAD)" -t moyai-local .
```

Export the service settings from this guide, then start the checked-out bundle:

```sh
cd deploy/docker
MOYAI_IMAGE=moyai-local sh moyai.sh up
```

Release
images contain the actual Git revision; do not override it to bypass admission.
The Docker Node stage runs `npm ci` and `npm run build:native`. The runtime image
contains generated assets and the shared, locked Python environment, without
Node or `node_modules`.

The release workflow promotes the existing AMD64/ARM64 image for the release's
exact commit and attaches a digest-pinned installation bundle. Before publishing
a GitHub release, wait for that commit's `Docker startup` main-push run to finish
successfully, including quickstart image publication. The release checks both
architecture revision labels and anonymous access, then copies the complete
image index (including attestations) without rebuilding it. A missing image,
failed or unfinished Docker run, or conflicting release image tag stops publication.
That workflow does not
deploy Moyai or migrate a customer's data. A [Helm chart](../../charts/moyai/README.md) is published alongside the image; AWS provisioning remains external.

## Publish the quickstart image

The root README's `docker run` quickstart uses the same application image with
the `moyai dev` command. It keeps SQLite, encrypted objects and a Temporal
development server in the local data volume. This local trial is separate from
the production installation bundle above.

The **Docker startup** workflow first runs its real-container startup,
authentication and persistence checks. A successful run on `main` then publishes
`ghcr.io/berriai/moyai:quickstart-sha-<full-git-sha>` for AMD64 and ARM64. It advances
the `quickstart` tag only if that revision is still the current main commit.
Pull requests never publish images. Maintainers can also run this workflow
manually on `main`.

The workflow uses GitHub's scoped `GITHUB_TOKEN` with `packages: write`; customers
should not need a GitHub token to pull the image. For the first publication:

1. Merge the workflow and let the Docker startup check and image build finish.
2. In the BerriAI `moyai` container package settings, set package visibility to
   **public** and ensure this repository has Actions access to the package.
   New GHCR packages may initially be private.
3. Rerun the publication job if its anonymous-access check failed while the
   package was private.

Publication is not ready for the public quickstart until the final check confirms
anonymous access to both CPU architectures. Until then, use the README's local
build fallback. A registry push alone does not establish that customers can pull
the image.

The `quickstart` tag follows main and is intended for local trials. For production,
use a published release's digest-pinned bundle and its maintenance procedure.
Publishing the quickstart image does not create a GitHub release or deploy an
application.
