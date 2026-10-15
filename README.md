# Lambda packaging regression evidence

Screenshots of browser-rendered backend evidence reports derived from actual ZIP contents and captured pytest output. These are local packaging/test results, not product UI or proof of a successful Docker/AWS build.

Baseline: 6ed37e196f27fce06a3241edc67ffa2b63f8cad9, with the new regression tests.
Fixed implementation: 188164616f0e8f0f608fd0d0c29fdb469605d824.

Before: 29 sandbox files, 0 agent files; 2 archive tests fail.
After: 29 sandbox files, 70 agent files; both archive tests pass.
Full Lambda CI pytest selection: 348 tests pass.

Local Docker verification was blocked before the build by a containerd storage I/O error.
