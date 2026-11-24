# Long-task evidence retention

Native conversations compact at 75% of the resolved model context window,
preserve 20 recent messages, and retain the SDK's saved-history receipts.
The summarizer still receives at most 500 characters per older tool result.
For long results, Moyai uses the first 200 characters and the trailing outcome,
with an explicit omitted-middle notice. This preserves command context and
common trailing test summaries, exit codes, paths and artifact references without
increasing the summarizer's tool-result budget.

Only the summarizer's input view changes. Recent tool results and stored
conversation snapshots retain their original content and types. The summary is
secondhand evidence: when a fact falls in an omitted section, retrieve the
original saved history rather than infer that the command succeeded. This is
not a guarantee that arbitrary middle-of-log evidence survives in a summary.

Existing Temporal histories retain their original compaction behavior through
the `moyai-compaction-tool-evidence-v1` patch marker. The implementation uses the
SDK's public `compact` method and retains its summary activity, tool-call pairing,
usage accounting and receipts.

Supplementary regression checks:

```sh
uv run pytest native/tests/test_compaction.py
```

These checks cover trailing failure evidence, unchanged recent structured
results, preserved user constraints, and old/new Temporal replay. They are
separate from actual-application testing with a configured model.
