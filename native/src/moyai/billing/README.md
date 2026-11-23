# Billing

`product_billing.py` owns canonical billing identities and arithmetic.
`product_billing_routes.py` owns the authorized report interface and HTTP
bindings. `billing_storage.py`, `billing_storage_cutover.py` and
`billing_storage_models.py` own the ledger, backfill and physical consolidation
protocol.

Storage operations retain their existing transactions, fences, schema versions,
source identities and explicit `--apply` requirement. The supported
`python -m moyai.billing_storage` command remains at the runtime root as a CLI
entrypoint; runtime imports use the implementation in this package directly.

The initializer performs no imports or I/O. Existing `test_product_billing`,
`test_billing_storage*`, schema and deployment tests cover the interfaces.
