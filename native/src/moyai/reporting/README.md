# Reporting

`analytics_service.report` is the report interface used by Settings. It composes
report arithmetic, identity attribution, canonical billing reads and recorded
performance observations. `analytics.py` remains database-independent.

The billing package owns ledger access and accounting failure policy. Reporting
does not write billing records or start execution. `performance.py` at the
runtime root records timing evidence; this package only projects it.

Import individual modules. The package initializer performs no imports or I/O.
The existing `test_analytics_*`, `test_native_analytics`,
`test_native_performance` and timing suites exercise the interfaces; the feature
package test prevents arithmetic from depending on the runtime or database.
