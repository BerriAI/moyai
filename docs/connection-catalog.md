# Connected-tool catalogs

Moyai discovers connected tools through Pydantic AI's local `search_tools`
capability. Selected connections remain available, but their full schemas are
deferred until a search reveals matching tools. The model can discover additional
tools later in the same turn. GitHub definitions identify their selected repository
so a search can distinguish otherwise identical tools from different connections.

The local search path is intentional. Provider-native search can still upload the
entire deferred corpus on every inference request. Moyai sends the local search
tool and the revealed definitions instead; ordinary workspace tools retain their
existing loading behavior.

## Schema reuse

Each worker keeps a bounded, in-memory catalog containing tool definitions and
retry limits, without credentials, live transports, executable tools, or validators.
The key includes the complete admitted turn scope: actor, turn, model, visibility,
surface and grant snapshots. Delegates with that same scope can reuse it.

Canonical product catalogs containing GitHub additionally reuse definitions across
turns in the **same session**. This is an explicit opt-in for Moyai's adapters:
their REST/publication schemas are fixed, and GitHub read schemas come from the
selected account. Their definitions do not depend on a turn ID. Every other
admitted identity/authority field remains in the key, including actor, product identity version, model,
visibility, surface, delegation provenance and complete grant/policy snapshots.
Arbitrary provider catalogs and catalogs without GitHub retain the turn key.

Every canonical catalog listing checks the current running turn and account
authority before returning definitions, including cache hits and after discovery
awaits. Failed authorization evicts the shared entry. Execution reconstructs the
current turn's validators and publication closures; approvals, effect receipts,
credentials and provider transports are never reused across turns. Credential
refresh and provider permission checks still occur when tools execute.

Entries expire after five minutes and the cache is limited to 32 catalogs and
16 MB of serialized definitions. Concurrent discovery of the same scope shares
one load. Failed or cancelled loads cannot publish a partial catalog. A new worker
can reconstruct an empty cache normally.

The SDK still records its normal discovery activities at each model step; a cache
hit avoids provider discovery inside that activity. This does not eliminate all
Temporal serialization or activity scheduling overhead.

## Execution and authorization

Provider construction and credential access stay inside Temporal activities.
Replay consumes the recorded results without contacting a provider.

Execution reconstructs only the selected connection's native tools and validates
arguments against the live definition. A changed schema, approval kind, metadata,
or removed tool fails closed and invalidates the cached catalog. Application
authority checks and native provider permission checks remain in place. A cached
schema never grants permission to execute a tool.

Catalogs with custom argument-validator callbacks bypass caching so their live
hooks remain visible to the SDK. Providers are not held open between activities.
The cache supports the stable connection catalogs wired by Moyai; it is not a
general cache for arbitrary context-dependent tool preparation.

## Verification

From the repository root:

```sh
uv run --frozen pytest -q \
  native/tests/test_connection_catalog.py \
  native/tests/test_temporal_connections.py \
  native/tests/test_product_connections.py \
  native/tests/test_temporal_runtime.py
```

The connection tests run the native agent through a local Temporal server and
replay its history. They verify selective discovery, two reads after one catalog
load, targeted reconstruction with another connection selected, and preservation
of canonical read/write tool policy. Cache tests cover validation, live revocation,
metadata changes, actor/grant isolation, expiry, eviction and cancelled discovery.
Canonical catalog tests also compare actual adapter definitions across turns,
check session/identity/provenance isolation, and verify fresh execution context,
credential rotation, revocation during discovery, and invalidation on schema drift.

For canonical PostgreSQL authority coverage, point `MOYAI_TEST_POSTGRES` at a
disposable local database and run `native/tests/test_product_authority.py`.

Selective discovery adds a model/tool round trip the first time a connected tool
is needed. Small tasks with few tools may therefore take longer despite sending
smaller requests. The main benefit is avoiding repeated provider catalog loads
and oversized tool lists in tasks with many selected connections or model steps.
