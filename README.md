# Moyai

Moyai is a shared AI workspace for coding and connected-service tasks. Chat with
an agent, steer its work, answer questions, inspect its computer, and keep the
resulting files and history. Pydantic AI and Harness provide the agent; Temporal
runs durable execution, Modal provides its computer, and LiteLLM supplies models.

The application has one Python installation, one lockfile, and one React frontend.
An existing deployment keeps its users, permissions, historical sessions,
credentials and accounting through the shared product services.

![Moyai home with the session sidebar and task composer](docs/assets/native/home.png)

*Current frontend with synthetic accounts and session data.*

## Deploy with Docker

Set your service endpoints and credentials, then run `sh moyai.sh up`. The package
contains one Moyai image with the backend, UI and worker code, plus Compose files
that start the application services. The host needs Docker Compose 2.30 or newer;
Python and Node run inside the build/application containers.

Bring a PostgreSQL database, private S3-compatible storage, a model gateway,
Modal credentials, and a reachable HTTPS origin. Use fresh, dedicated application
schemas for a new installation. Existing deployments should follow the
[migration and operations guide](docs/production-deploy.md).

### 1. Get the deployment package

For a GitHub release that includes `moyai-docker.tar.gz`, download that asset and
extract it:

```sh
tar -xzf moyai-docker.tar.gz
cd moyai-docker
```

The bundle selects the release image by immutable digest. If a release bundle is
not yet available, build from a checkout instead, starting at the repository root:

```sh
docker build --build-arg MOYAI_BUILD_SHA="$(git rev-parse HEAD)" -t moyai-local .
export MOYAI_IMAGE=moyai-local
cd deploy/docker
```

### 2. Set the environment variables

Replace these placeholders with your service settings, or supply them through
your secret manager:

```sh
export MOYAI_INSTALLATION_ID='moyai'
export MOYAI_PUBLIC_URL='https://moyai.example.com'
export MOYAI_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE'
export OBJECT_STORAGE_BUCKET='your-private-bucket'
export OBJECT_STORAGE_REGION='us-east-1'
export OBJECT_STORAGE_ACCESS_KEY_ID='YOUR_STORAGE_ACCESS_KEY'
export OBJECT_STORAGE_SECRET_ACCESS_KEY='YOUR_STORAGE_SECRET_KEY'
export LITELLM_API_BASE='https://your-gateway.example.com/v1'
export LITELLM_API_KEY='YOUR_MODEL_KEY'
export AGENT_MODEL='YOUR_RESPONSES_MODEL'
export MODAL_TOKEN_ID='YOUR_MODAL_TOKEN_ID'
export MODAL_TOKEN_SECRET='YOUR_MODAL_TOKEN_SECRET'
```

For S3-compatible storage, also set `OBJECT_STORAGE_ENDPOINT` to its endpoint.
For AWS workload credentials, omit the two storage-key exports. Use addresses
reachable from the containers; `localhost` inside a container refers to itself.

Choose **one Temporal option** before the first startup:

| Option | Configuration |
| --- | --- |
| Temporal Cloud or an existing server | Set the address and existing namespace, with the server's TLS/authentication settings. |
| Include Temporal in this deployment | Set `MOYAI_TEMPORAL_MODE=bundled`; the package starts Temporal and its persistent database and generates the internal database password. |

For Temporal Cloud using an API key:

```sh
export MOYAI_TEMPORAL_MODE='external'
export TEMPORAL_ADDRESS='YOUR_TEMPORAL_HOST:7233'
export TEMPORAL_NAMESPACE='YOUR_TEMPORAL_NAMESPACE'
export TEMPORAL_TLS='true'
export TEMPORAL_API_KEY='YOUR_TEMPORAL_API_KEY'
```

Or, for bundled Temporal, omit those external connection settings and use:

```sh
export MOYAI_TEMPORAL_MODE='bundled'
```

Standalone is the default. To run the API, coordinator, broker and worker as four
containers using the same image, also export `MOYAI_INSTALL_TOPOLOGY=split`.
Choose the topology before creating the installation; changing this variable
does not migrate existing data.

Point your HTTPS ingress at the Docker host's proxy, which binds to
`127.0.0.1:8787` by default. Preserve the Host header and support streaming and
WebSockets. For ingress on another host, configure `MOYAI_BIND_ADDRESS` as
described in the [full installation guide](deploy/docker/README.md).

### 3. Start Moyai

From the deployment directory, with the variables exported:

```sh
sh moyai.sh up
```

The helper creates private configuration, checks dependencies, prepares schemas,
retains generated signing/encryption keys, starts the selected services, and
waits for activation. No separate wizard or setup command is required.

Open `MOYAI_PUBLIC_URL` and sign in with `WORKSPACE_PASSWORD` if you supplied it.
Otherwise, retrieve the generated password in your private terminal:

```sh
sh moyai.sh password
```

Use `admin` if the sign-in form asks for a username. Ask Moyai to create a file and
save it for download to verify the model and Modal computer; service readiness
alone does not run a task. `sh moyai.sh status` checks startup readiness.

Keep credentials exported for later starts and retain the deployment directory
and persistent volumes. Repeated `up` uses the saved configuration. For optional
interactive configuration, diagnostics, backups and upgrades, see the
[Docker installation guide](deploy/docker/README.md).

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Python 3.13,
and Node.js 22 or newer. From the repository root:

```sh
uv sync --frozen
cp .env.example .env
chmod 600 .env
```

Installation also builds the frontend from source. Edit `.env` with your workspace password, LiteLLM gateway URL/key, enabled model
alias, and Modal token pair. The model alias must support the gateway's Responses
API. An authenticated local Modal CLI profile also works for source development.
Leave `ENCRYPTION_KEY` empty on a new local installation to generate and persist
one in `DATA_DIR`; preserve that directory and its key on subsequent starts.

```sh
uv run --frozen moyai dev --port 8787
```

Open <http://127.0.0.1:8787> and sign in as `admin` with `WORKSPACE_PASSWORD`.
The command starts a local Temporal server, worker and API, using real configured
inference and Modal. It may download Temporal and build the first computer image.
Model and computer usage are billed by those providers.

Try: “Create `/workspace/hello.py` that prints Hello from Moyai, run it, and save
it for download.” Success means the chat reports the observed output and a saved
file opens in the Files panel. Follow up in the same conversation to change it.
If inference fails, verify the model alias through your gateway's Responses API;
if computer creation fails, check Modal authentication and the worker log.

For a Docker-based local trial, use the same `.env` and run:

```sh
docker compose up --build
```

This starts the same development service at port 8787 and keeps application data
and Temporal history in the `workspace-data` volume. Keep that volume to preserve
history. Local development is a single-host setup; it is not the production
release procedure.

## Use and operate

- [Using Moyai](docs/usage.md): chat, files, computer control, connections, Settings,
  Slack, approvals and recovery.
- [Architecture](docs/architecture.md): how requests move through the system and
  which data belongs to each service.
- [Production deployment](docs/production-deploy.md): existing-product setup,
  guarded maintenance, upgrade checks and recovery.
- [Verification](docs/verification.md): tests, packaging and Docker checks.
- [Consolidation scope](docs/consolidation.md): retired code, preserved contracts
  and features that remain deferred.
- [Documentation index](docs/README.md).

## Develop

```sh
uv run --frozen pytest -q tests
uv run --frozen pytest -q native/tests
uv run --frozen --directory native ruff check src tests
uv run --frozen --directory native pyright
npm run typecheck
npm test
npm run test:ui
```

Install Chromium once with `npx playwright install chromium`. PostgreSQL tests
need a disposable local database; see the verification guide. Build frontend changes with `npm run build`. The generated
`native/src/moyai/static` directory is ignored by Git and included by the package build.
The Python image serves those assets without Node.js.
