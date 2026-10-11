# Agent startup separation

The goal is to let the selected task agent reason and use eligible broker tools
before an execution sandbox is ready. The first incremental PR overlaps source
loading with durable cold acquisition. **Sandbox-free inference is not implemented
by that PR.** There is no greeter model, changed prompt, alternative harness or
new production service.

## Current path and boundaries

Reviewed against `945983c766ee608ad51e5eefd1df086d7957a00f`. The earlier prepared-pool,
Codex runtime reuse, dispatch wake and separate-broker work is already on main.

| Step | Owner | Needs the execution sandbox? |
| --- | --- | --- |
| Persist input and dispatch a workflow wake | `Store`, `TemporalRunManager.submit_in` | No |
| Admit capacity, claim input, retain requester/model | `DurableRunner.advance`, `begin_turn` | Capacity is currently coupled to every cold turn |
| Read source and attachments | `Slack.prepare`, `SlackFiles.prepare` | No; retryable reads with current access checks |
| Pin repository environment, restore/create workspace | `DurableRunner.provision`, `Environments.prepare`, provider adapter | Yes for restore/create; metadata checks are control-plane work |
| Refresh runtime and restore browser; assemble launch spec | `DurableRunner._step` install, `RunManager.spec` | Files/browser need the sandbox; database reads do not |
| Start durable supervisor and agent | `DurableRunner.command`, `sandbox/agent.py` | Currently yes |
| Prepare repository, attachments, journal and SDK | `sandbox/agent.py`, native harness adapters | Currently uses `/workspace`, `/session` and native subprocesses |
| First model request | SDK → `BrokerRelay` → authenticated broker → gateway | Inference itself does not need the workspace, but the SDK currently lives there |
| Save answer, archive/checkpoint, settle and deliver | broker/runner durable receipts and Slack outbox | Conversation/delivery need not inherently require a filesystem; current completion does |

Source reads and cold provisioning now overlap. Runtime upload and independent
launch-spec reads already overlap. Neither overlap makes model inference start
before sandbox readiness. Controller monitor spans also overlap actual execution
and must not be added to inference durations.

The separate broker retains its existing responsibilities and ownership fences.
It separates inference traffic from coordinator restarts; it does not host the
agent loop. No generated code or SDK-native file/shell tool is moved to that host.

## Compatibility of this prerequisite

| Path | Behavior | Evidence |
| --- | --- | --- |
| Temporal cold turn, all existing harness selections | Source/acquisition overlap before existing launch | Real controller and database tests; Codex launch-spec fixture |
| Modal acquisition | Same names, capacity and saved workspace selection | Deterministic provider tests, including lost ACK and late completion |
| Lambda/Substrate acquisition | Same provider-independent controller; adapters unchanged | Existing provider/environment regression tests; no live provider qualification |
| Warm workspace, prepared-pool hit | Existing source-before-install ordering | Warm/pool regression tests |
| Demo, non-Temporal `RunManager` | Existing behavior | Existing runner/durable coverage |
| Agent before sandbox, any harness/provider | Not supported yet | Requires the subsequent stages below |

## Subsequent mergeable stages

1. Make the public conversation/tool-receipt journal durable independently of
   `/session/context.sqlite3`. Preserve its epoch/cursor, unresolved outcomes,
   compaction coverage and requester privacy rules. Keep native SDK transcripts
   optional and scoped separately. Test restart and no-filesystem settlement
   before moving execution.
2. Establish a supervised agent runtime with a tool bridge for the mainline Codex
   selection. Audit native shell, patch, image/file access, SDK subprocesses and
   code-mode execution. Disabling a custom shell tool is insufficient. Generated
   commands must execute only in the authorized execution sandbox. Preserve model
   and reasoning settings and the real turn's inference accounting.
3. Route the first computer-dependent call through one durable acquisition owner.
   On-demand and optional eager requests must share the existing reservation,
   leases, cancellation, environment pin and provider creation journal. Classify
   tools individually: broker-native connected-app reads/writes are candidates;
   shell/files, browser, checkout and tools consuming workspace files need the
   computer. A tool name or catalog source alone is not proof of independence.
4. Settle no-sandbox turns through conversation, billing and delivery receipts
   without inventing a filesystem checkpoint. Exercise steering, requester/model
   changes, credential and agent waits, stop and restart, and uncertain writes.
5. Qualify an opt-in supported harness/provider combination with real model and
   real sandbox runs. Compare matched cold and warm samples: submission to first
   model request, visible model output, useful action, computer wait, completion,
   errors and cost. Keep the existing execution path as the staged-rollout fallback.

Keep prompt simplification, memory-quality and skill-discovery experiments
separate. Match coordinator/broker/worker builds, retain authentication and spend
ownership, and do not deploy as part of implementing these PRs.
