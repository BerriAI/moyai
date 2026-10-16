# Verification

Run commands from the repository root. Install the one workspace environment and
frontend dependencies before testing:

```sh
uv sync --frozen --extra postgres-migration
npm ci
npx playwright install chromium
```

## Python and PostgreSQL

Use a disposable local UTF-8 PostgreSQL 17/18 server. The test role needs permission
to create/drop isolated schemas and, for native qualification, databases. Export
`MOYAI_TEST_POSTGRES_URL` for shared/release tests and `MOYAI_TEST_POSTGRES` for
native tests to that server's connection URL. Never use a production database.

```sh
uv run --frozen --extra postgres-migration pytest -q tests --tb=short
uv run --frozen pytest -q native/tests --tb=short
uv run --frozen pytest --postgres-backend -q tests/test_database_boundaries.py tests/test_message_snapshot_pool.py
uv run --frozen --extra postgres-migration python scripts/postgres_migration_smoke.py
uv run --frozen python -m scripts.online_migration_probe --output /tmp/moyai-online-migration
uv run --frozen ruff check --config native/pyproject.toml native/src native/tests
uv run --frozen pyright --project native
uv build --package moyai-pydantic
```

The native suite uses the real SDK, local Temporal, stores and encryption with
controlled models. It covers execution, replay, cancellation, authorization,
receipts, restoration and product adapters. Schema tests retain historical
manifest digests and verify preserved rows without importing retired engines.
Release tests exercise the real controller with simulated Render/SSH responses
and real PostgreSQL guards. Package tests build a wheel from the source archive
and inspect resources outside the source checkout.

Without the test database environment, PostgreSQL cases skip. A passing local
SQLite-only run is not full qualification. Native tests may download Temporal
binaries and take around 15 minutes; avoid editing runtime code during a full run.

## Frontend and image

```sh
npm run typecheck
npm test
npm run build
npm run test:ui
git diff --exit-code -- native/src/moyai/static
docker build -t moyai-local-test .
python3 scripts/docker_smoke.py moyai-local-test
```

The asset-drift command expects source and generated assets to have been committed.
Browser tests use synthetic API data with Chromium at desktop, tablet and mobile
sizes. Docker smoke uses disposable containers and volumes: it verifies login,
CSRF, credential redaction, persistent sessions/connections across restart, UID and
mounted-directory ownership, symlink handling, staging, and invalid distributed
configuration. It makes no model or cloud-computer requests.

CI runs these checks in the frontend, native runtime, PostgreSQL runtime/migration,
production release rehearsal and Docker workflows. The release gate requires the
exact selected main commit's checks, including docs-only changes.

## Deployment evidence

Local tests do not certify every live provider token, production identity policy,
cloud image build or Render handoff. Before accepting a deployment, check the
actual four roles, original-user sign-in, historical files, a real model/Modal
task, continuation/approval recovery and the release receipt. Preserve uncertain
external effects for explicit reconciliation. No deployment is performed by the
consolidation test suite.
