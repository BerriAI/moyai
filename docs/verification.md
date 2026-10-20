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
`MOYAI_TEST_POSTGRES_URL` for shared runtime tests and `MOYAI_TEST_POSTGRES` for
native tests to that server's connection URL. Never use a production database.

```sh
uv run --frozen --extra postgres-migration pytest -q tests --tb=short
uv run --frozen pytest -q native/tests --tb=short
uv run --frozen pytest --postgres-backend -q tests/test_database_boundaries.py tests/test_message_snapshot_pool.py
uv run --frozen --extra postgres-migration python scripts/postgres_migration_smoke.py
uv run --frozen python -m scripts.online_migration_probe --output /tmp/moyai-online-migration
uv run --frozen --directory native ruff check src tests
uv run --frozen --directory native pyright
uv build --package moyai-pydantic
```

The native suite uses the real SDK, local Temporal, stores and encryption with
controlled models. It covers execution, replay, cancellation, authorization,
receipts, restoration and product adapters. Schema tests retain historical
manifest digests and verify preserved rows without importing retired engines.

The required native quality job also runs the Slack conversation contract by
explicit test name. It invokes Moyai in a pre-existing human thread and checks
the prepared model input for the parent link and preview. Preserve this behavior
test when replacing a runtime; tests for signatures or reply delivery are not a
substitute. A deliberate behavior change must update the contract and document
the user-visible change, rather than delete the test as legacy-only.

Slack tests exercise the installed AgentChat SDK, with only provider HTTP replaced
by test responses. AgentChat owns event normalization, bounded thread context and
message payloads. Moyai owns current grants, session admission, the encrypted
input snapshot and the durable outbox; preserve these boundaries when upgrading
the SDK. Source installation requires Rust 1.88+; the Docker dependency builder
compiles AgentChat without adding a Rust toolchain to the runtime image.

Runtime rehearsal tests exercise startup, shutdown and real PostgreSQL migration
guards. Deployment-controller tests live in a private repository. Package tests build
a wheel from the source archive and inspect resources outside the source checkout.

Without the test database environment, PostgreSQL cases skip. A passing local
SQLite-only run is not full qualification. Native tests may download Temporal
binaries and take around 15 minutes; avoid editing runtime code during a full run.

## README composer screenshots

The browser fixture renders the real frontend with synthetic accounts, sessions,
and model labels. It fills an unsent draft and widens the sidebar using its
keyboard control. Regenerate both theme variants from the repository root:

```sh
MOYAI_README_CAPTURE_DIR=docs/assets/native node --test \
  --test-name-pattern='README composer demo' tests/browser/native_product_shell.cjs
```

Captures are 2560 × 1600 PNGs (1280 × 800 CSS pixels at 2×). The capture waits for
fonts and fails if General Sans did not load, rather than silently saving a
fallback-font image. Fontshare must be reachable. Inspect both images at README
size before committing. These images demonstrate presentation, not live model
or connected-service execution.

## Frontend and image

```sh
npm run typecheck
npm test
npm run build
npm run test:ui
test -z "$(git ls-files -- native/src/moyai/static)"
docker build -t moyai-local-test .
python3 scripts/docker_smoke.py moyai-local-test
```

The asset check ensures generated frontend files have not been committed.
The package test builds from a fresh source tree and verifies the resulting assets.
Browser tests use synthetic API data with Chromium at desktop, tablet and mobile
sizes. Docker smoke uses disposable containers and volumes: it verifies login,
CSRF, credential redaction, persistent sessions/connections across restart, UID and
mounted-directory ownership, symlink handling, staging, and invalid distributed
configuration. It makes no model or cloud-computer requests.

CI runs these checks in the UI unit, frontend, native runtime, PostgreSQL runtime/migration,
runtime rehearsal and Docker workflows. Managed deployment requirements are
maintained in a private repository.

## Deployment evidence

Local tests do not certify every live provider token, production identity policy,
cloud image build or Render handoff. Before accepting a deployment, check the
application health, user sign-in, historical files, a real model/Modal
task and continuation/approval recovery. Preserve uncertain
external effects for explicit reconciliation. No deployment is performed by the
consolidation test suite.

## Coding computer Node runtime

`native/src/moyai/coding_image.py` owns the exact Node version and the official
AMD64/ARM64 archive checksums. Fresh and restored Modal project images use its
installer, and `scripts/lambda_image.py` includes that same installer in the
Lambda image package. Install Node before any image-bundled npm dependencies.
Do not add a separate Node pin to a preview launcher.

The bundled npm configuration enables `engine-strict`, so an incompatible
dependency reports an install error instead of leaving a partially usable
preview. A repository that requires another Node version must select that
runtime before both installation and execution. Changing the image pin rebuilds
the Node layer and bundled dependencies. Existing live computers and filesystem
checkpoints retain their environments; reinstall project dependencies from the
lockfile when changing their runtime.

With Docker available, run:

```sh
uv run --frozen python -m scripts.node_runtime_smoke --platform linux/amd64
uv run --frozen python -m scripts.node_runtime_smoke --platform linux/arm64
```

The native runtime CI runs both architectures. It builds the complete packaged
Lambda image, installs the repository's locked dependencies, loads Tailwind's
native binding, and starts Vite through npm. The install hook, npm and preview
must all report the pinned Node executable/version. It also checks that an
unsupported Node engine fails installation. These container checks supplement
actual-app model/Modal verification.
