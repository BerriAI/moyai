# PostgreSQL CI coverage

`PostgreSQL runtime` runs six focused jobs on relevant pull requests and every
push to main. The job IDs and workflow filename stay stable for release checks.

| Job | What must stay on the PR path |
| --- | --- |
| `runtime` | SQL bindings, rollback, constraints, atomic message claims, concurrent schema initialization, ownership fencing, imports, authentication during writes, and real HTTP persistence across a server restart. SQLite admission is also retained. |
| `storage` | Payload maintenance and migration, with recovery from shared storage in a replacement process. |
| `scheduling` | Database writer/pool contention, responsive scheduling, and real Temporal restart behavior. |
| `distributed` | Shared admission, execution fencing, Slack delivery ownership, cancellation, and startup/cleanup/Temporal contention probes. |
| `broker` | Broker isolation, offline schema migration, startup fences, overlapping APIs, and context recovery. |
| `rolling` | Owner compatibility, revocation, handoffs, stream draining, and replacement of all four service roles. |

The separate `PostgreSQL migration rehearsal` workflow owns the full SQLite-to-
Postgres migration suite. It is no longer repeated inside `runtime`. The default
SQLite session, responsiveness, Slack mirror, and spend suites remain in
`Docker startup` instead of also running in the Postgres runtime job.

## Broad compatibility and load checks

`PostgreSQL compatibility` runs daily at 09:23 UTC and on manual dispatch. It
retains the 51 general feature test files previously rerun against Postgres in
the main runtime job. Four groups cover chat, settings/features, workers, and
models/swarms, with at most two running at once. The worker group also runs the
3,000-session, four-process capacity probe. PostgreSQL admission and agent tests
already run in `distributed`, so the broad sweep does not duplicate them.

To request the full sweep, select **Actions → PostgreSQL compatibility → Run
workflow**, choose the branch, and run it. The workflow must first exist on the
default branch for GitHub to expose manual dispatch. Each group fails normally
on an error; there is no `continue-on-error` or passing placeholder check.

Feature-specific Postgres regressions may therefore be detected by the daily
sweep rather than on every PR. Changes to database operations should add focused
coverage to the relevant PR job; adding a general feature suite should not expand
the database gate automatically. This keeps the PR job from growing back into a
serial replay of the application suite.

Superseded pull-request runs are cancelled. Main-branch runs are preserved because
release tooling requires successful checks for the selected commit.

## Local runtime check

Start a disposable PostgreSQL 17 database and set `MOYAI_TEST_POSTGRES_URL` to its
connection URL. From the repository root, run the same commands as CI:

```sh
uv run --frozen --python 3.13 pytest -q --tb=short tests/test_admission_scaling.py
uv run --frozen --extra postgres-migration --python 3.13 pytest -q --tb=short tests/test_postgres_runtime.py
uv run --frozen --python 3.13 pytest --postgres-backend -q --tb=short tests/test_cloudflare_login.py -k 'conversation or keeps_personal_tools'
uv run --frozen --python 3.13 python scripts/postgres_runtime_smoke.py
```

Tests and probes use isolated schemas and synthetic data; do not point them at a
production database.
