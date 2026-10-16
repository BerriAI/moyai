# CI execution and deployment gates

Production deployment still waits for successful CI on its exact selected main
commit. Parallel jobs shorten that wait without omitting tests or changing the
maintenance rollout.

## Native runtime

The native workflow runs lint, type checking and package builds in one job and
the full pytest collection in four isolated test jobs. Each test job has its own
PostgreSQL service. `pytest-split` assigns every collected test to one group;
`least_duration` distributes tests evenly by count when no duration history is
available. Every shard uses the same checkout, lockfile and splitting settings.

Each shard stops on its first failure and publishes its JUnit report. Other
shards continue, so their independent results remain available. The stable
`runtime` check succeeds only when quality checks and all shards succeed;
failure, cancellation and skipped prerequisites do not satisfy it.

Run the complete suite locally from `native`:

```sh
uv sync --frozen
uv run pytest -q --tb=short
```

Reproduce one CI shard by choosing a group from 1 through 4:

```sh
uv run pytest -x -q --tb=short --durations=20 \
  --splits 4 --group 1 --splitting-algorithm least_duration
```

Configure `MOYAI_TEST_POSTGRES` with an isolated local PostgreSQL instance to
include database qualification, as CI does. Add `--collect-only` to inspect the
assignment without running tests. The workflow retains per-shard JUnit artifacts
and prints the slowest tests to help evaluate balance from actual runner timings.

## Docker startup

The production image build and actual container startup/restart checks run at the
same time as the five regression groups. The stable `docker` check runs afterward
and requires both the entire regression matrix and image validation to succeed.
It rejects failed, cancelled and skipped prerequisites.

No production deployment is triggered by this workflow. The release controller's
existing exact-commit CI checks and maintenance sequence are unchanged.
