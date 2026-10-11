# Diagnosing slow replies

The agent trace includes body-free `runtime.*` child spans for durable controller
steps. These use the same run and turn identity as model/tool spans:

- `runtime.activity_queue`: Temporal's current-attempt scheduled-to-start time.
- `runtime.prepare`: startup preparation; source context for warm, pool and demo turns.
- `runtime.provision`: workspace provisioning or environment preparation. On cold
  turns this now overlaps source context loading and waits for both branches.
- `runtime.install`: runtime refresh, browser restore, and launch-spec preparation.
- `runtime.launch`: starting the sandbox supervisor.
- `runtime.monitor`: observing the sandbox process, including intentional polling.
- `runtime.save`: capture/archive persistence and filesystem checkpointing.
- `runtime.checkpointed`, `runtime.cleanup`, `runtime.finish`: result settlement
  and workspace lifecycle work.

Other durable steps use their existing phase names. Each span records
`moyai.runtime.phase`, `moyai.runtime.segment`, and
`moyai.runtime.duration_ms`. Failed steps mark the span as failed without
including exception bodies. Tracing failures must not replace a task result.

These spans are controller intervals, not model calls or tool calls. In
particular, monitor intervals overlap inference and sandbox work. Do not add
them to model durations to calculate total latency. Repeated phases can be
separate attempts or resumed segments. Queue spans measure activity queueing,
not the earlier delay from Slack receipt to turn admission. Existing scheduling
logs retain activity attempt IDs and wake-delivery measurements.

For the historical `hello`, `can see this`, and `noice` cases, repeat the same
requests on the same model and deployment, separating cold workspaces from warm
follow-ups. Compare the root span, model spans, runtime intervals, and scheduling
logs to locate the uncovered time before changing startup or persistence.
Root duration does not measure Slack delivery. Historical traces cannot gain
these new spans retroactively; instrumentation alone establishes no speedup.
An interrupted cold acquisition can leave pending source reads for
`runtime.install`; that recovery interval includes those reads. A shorter
`runtime.prepare` alone is not evidence of faster model output: compare submission
through the first actual model span and delivery using the same task/settings.
