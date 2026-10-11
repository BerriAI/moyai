# Choose checks for the changed boundary

Use current `.github/workflows/*.yml` and the adjacent tests as the source of truth.
The following are starting points, not a requirement to run every suite.

| Affected behavior | Useful local checks |
| --- | --- |
| Skill discovery, revision pinning or bundles | `uv run --frozen --python 3.13 pytest -q tests/test_skills.py tests/test_initial_context.py tests/test_bundled_skills.py` |
| Agent package imports or core prompt | `uv run --frozen --python 3.13 pytest -q tests/test_agent_package.py tests/test_pr_delivery.py` |
| GitHub PR and CI tools | `uv run --frozen --python 3.13 pytest -q tests/test_github_ci.py tests/test_pr_milestones.py tests/test_github_write_access.py` |
| Browser/Computer lifecycle | `uv run --frozen --python 3.13 pytest -q tests/test_computer.py tests/test_idle_window.py tests/test_mcp_bridge.py` |
| Frontend controllers | Relevant `node --test tests/<feature>.cjs`, then `npm test` when shared behavior changed |
| React/shadcn sources | `npm run typecheck`, `npm run build`, `npm test`, then affected browser flows |
| Database/runtime compatibility | `tests/test_schema_migrations.py`, `tests/test_api_compatibility.py`, `tests/test_separate_broker.py` and the current PostgreSQL workflow |

Check that each named test exists on the branch. Python tests use `tests/conftest.py`;
the repository also has JavaScript tests, so a passing Python suite does not cover UI
controllers. The frontend CI installs Chromium before `npm run test:ui`.

## PostgreSQL behavior

Storage, permissions, transaction and concurrency changes need the corresponding
tests on real PostgreSQL as well as SQLite. `tests/conftest.py` supports:

```sh
uv run --frozen --python 3.13 pytest --postgres-backend -q tests/test_skills.py tests/test_bundled_skills.py
```

This requires `MOYAI_TEST_POSTGRES_URL` pointing to a **disposable local test
database**. The fixture creates and removes isolated schemas. Never point it at a
production or shared development database. Tests marked `sqlite_only` intentionally
skip on PostgreSQL; report the skip instead of counting it as a pass.

Production uses explicit schema migrations and separate runtime roles. A SQLite
test does not prove PostgreSQL locking or API/broker compatibility. If changing the
schema, inspect `app/schema_migrations.py`, existing migration tests and
`.github/workflows/postgres-runtime.yml` before choosing the rollout checks.

## Runtime proof

Exercise the actual changed path with safe data: an API request, a browser flow, a
CLI invocation or an existing smoke script. Check the observable result and relevant
failure path. Report synthetic providers separately from live integrations. Consult
the demo's own description before using its output as evidence.
