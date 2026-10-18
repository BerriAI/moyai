# CI execution

Public CI checks runtime behavior, packaging and self-hosted startup. Managed
deployment workflows, gates and runbooks live in a private repository.

## Native runtime

The native workflow runs lint, type checking and package builds in one job and
the full pytest collection in four isolated test jobs. Each test job has its own
PostgreSQL service. `pytest-split` assigns every collected test to one group;
`least_duration` balances estimated work using the checked-in
`native/.test_durations` file. New tests receive the mean recorded duration, so
they remain part of the suite. Every shard uses the same checkout, lockfile,
timing file and splitting settings. Timing data affects assignment only.

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
  --splits 4 --group 1 --splitting-algorithm least_duration \
  --durations-path .test_durations
```

Configure `MOYAI_TEST_POSTGRES` with an isolated local PostgreSQL instance to
include database qualification, as CI does. Add `--collect-only` to inspect the
assignment without running tests. The workflow retains per-shard JUnit artifacts
and prints the slowest tests to help evaluate balance from actual runner timings.

To refresh the timing file, download all four `native-tests-*` artifacts from one
successful **Native Pydantic runtime** run. From the repository root, run:

```sh
gh run download RUN_ID --pattern 'native-tests-*' --dir /tmp/native-test-results
python scripts/update_test_durations.py /tmp/native-test-results/native-tests-*/*.xml
```

Replace `RUN_ID` with that run's numeric ID and use an empty download directory.
The command reports the number of timings written. Commit the updated timing
file so every shard uses the same data. The initial timings come from public CI
run `39077937833`; they are scheduling estimates, not performance thresholds.

## Docker startup

The image build and actual container startup/restart checks run independently
of the shared-service, runtime rehearsal and native test workflows. The stable
`docker` check covers the image and startup proof. Retired-engine regression groups are
removed with their engines; current shared-service tests run in the PostgreSQL
runtime workflow.

BuildKit caches intermediate image layers between runs. Every run still builds
the selected checkout, loads the resulting image and runs all container smoke
checks. A missing cache falls back to a full build.

These public CI workflows do not deploy the managed application.
