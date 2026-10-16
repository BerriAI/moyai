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

## Install a released Docker package

Use the [Docker installation guide](deploy/docker/README.md) for a versioned bundle,
persistent storage and guarded upgrades. Confirm that the chosen GitHub release
contains the image and bundle; source on main alone does not establish publication.

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
