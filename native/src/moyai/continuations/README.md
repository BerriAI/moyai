# Historical continuation

`product_continuation.py` owns the authorized handoff from a historical
conversation to native execution. `product_continuation_routes.py` binds its
preview and mutation interfaces to HTTP. Workspace collection, restoration,
the guest script, errors, records and schema checks live beside it.

The restore module resolves `product_continuation_guest.py` next to itself in
both the source tree and installed wheel. The canonical `app` package still
owns original records and artifact formats. This package does not replay old
tool calls, rewrite original identities or replace historical URLs.

Import individual modules; the initializer has no effects. Existing
`test_product_continuation`, history handoff, private history, schema,
workspace recovery and packaging suites exercise this protocol.
