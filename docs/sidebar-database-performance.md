# Sidebar database performance

Status: proposed changes, not deployed. This patch targets the database work
behind `GET /api/history/sessions?source=all&scope=mine&limit=100`.

## Where the time goes

The production audit measured 58 sidebar requests: median total time **3,121 ms**,
median time to first byte **3,093 ms**, and median transfer time **11 ms**. The
sidebar contained 16 native and 43 legacy conversations. These measurements
identify server waiting as the dominant cost; they do not attribute individual
production milliseconds to SQL.

Disposable local PostgreSQL profiling reproduced two expensive operations:

1. The canonical ancestry view expands descendants recursively. PostgreSQL
   overestimates its fan-out and repeatedly sorts the graph. History reads
   expand that relation separately for ancestry and roots.
2. The ordered personal-history query expects to find a full page early. When
   relatively few roots belong in the viewer's sidebar, it can scan the visible
   history again for each root. More unrelated history makes the same sidebar
   substantially slower.

## Query changes

The PostgreSQL reader follows each run's unique parent using primary-key joins
and shares one statement-local ancestry expansion between roots and access
checks. It preserves self ancestry, excludes missing parents, and uses `UNION`
to terminate cycles. The stored views remain unchanged. Continuation and
historical Slack access checks use the same query helper.

The listing query materializes eligible navigation IDs and timestamps once,
orders and pages those IDs, then projects the sidebar fields. Privacy, deletion,
identity, continuation and Slack checks remain in place, including fresh access
revalidation. Pagination and response fields are unchanged. No authorization
result is cached across requests.

## Local measurements

The comparison uses PostgreSQL 16.15, canonical indexes, synthetic history,
one warm-up and seven measured reads per version. Each read returns exactly the
same complete response: 43 legacy and 16 native conversations. Unrelated history
remains in the database. The baseline uses the original listing, graph and
continuation queries from `a4600c79`, unchanged at the patch base `6ad1d34d`.

| Legacy runs | Seeded chain depth | Before: complete sidebar service | After | Speedup |
| ---: | ---: | ---: | ---: | ---: |
| 4,260 | 4 | 534 ms | 72 ms | 7.4× |
| 16,260 | 4 | 4,094 ms | 218 ms | 18.8× |
| 4,260 | 16 | 1,630 ms | 175 ms | 9.3× |

These are local backend medians, including both source reads, authorization,
revalidation and merging. In the largest case, measured history SQL fell from
4,044 ms to 181 ms. HTTP transport, browser rendering, production load and
Slack-provider delays are outside this measurement. A production browser
speedup has not been measured, and these results do not establish 10× for every
sidebar composition. Repeated Slack identity/audience reads remain a separate
optimization opportunity.

To reproduce from the repository root, use a running local PostgreSQL server
and a local account allowed to create disposable databases. The fixtures reject
non-local hosts and remove only the databases they create:

```sh
MOYAI_TEST_POSTGRES=postgresql://localhost/postgres \
MOYAI_BENCHMARK_SIDEBAR=1 \
uv run pytest -c native/pyproject.toml native/tests/test_sidebar_database.py -q -s
```

Expect passing tests and three `SIDEBAR_DATABASE` records with per-read samples
and before/after medians. Tests compare every response field, alternate old and
new readers against unchanged views, check graph equivalence and revocation,
and bound visible-history scans by graph size. Additional coverage exercises
pagination, archives, deleted sessions, private/deleted ancestors, cycles,
orphans, continuation access and historical Slack access.

## Deployment compatibility

This patch changes read queries only. Canonical schema revision, native migration
manifests, persisted views, dependencies and role compatibility contracts are
unchanged relative to its base. It introduces no migration or maintenance
requirement, so old and new readers can coexist during a compatible rolling
release. The final deployment mode still depends on the exact candidate's full
diff from the running release and the deployment workflow's preflight checks.
No live preflight or deployment has been performed for this patch.

After an authorized release, compare the same user's sidebar request timings,
complete session IDs, pagination, archives and private-session visibility.
Report the production browser result separately from these local measurements.
