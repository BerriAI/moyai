# `/goal`: persistent, guarded continuation

Start a cloud chat request with `/goal <objective>` (web or Slack thread chat):

```text
/goal fix the failing tests, verify the suite, and open a PR
```

This is a host-owned execution loop, not just a stronger prompt. When Hermes returns a normal final response without valid completion evidence, Moyai passes the returned conversation history into another `run_conversation` call in the **same user turn**. It does not enqueue synthetic user messages or repeat the original request. Ordinary non-goal conversations are unchanged.

The web command picker exposes `/goal` alongside scoped skills. An existing skill named `goal` remains accessible as `/personal:goal` or `/org:goal`.

## Controls

- `/goal status`: display the saved objective, state, continuation count and reason.
- `/goal pause` (also `stop`, `cancel`): disable automatic continuation without discarding the goal.
- `/goal resume`: resume an unfinished goal with a fresh safety-budget window.
- `/goal clear`: discard the goal.
- `/goal <new objective>`: replace the goal and invalidate old completion claims.
- `/goal` or `/goal help`: usage.

Commands are recognized only at the start of authenticated user input, not from quoted Slack context, tool output, old conversation history, code fences, or paths. Send-now corrections update the controller only after Hermes accepts delivery. The existing Stop button still revokes access and stops execution; this feature cannot restart it. A recovered active goal outside an explicit checkpoint continuation loads paused and needs `/goal resume`.

Goal controls currently use the existing sandbox turn lifecycle, so an idle status/control request still requires a cloud machine. This is not a new Slack workspace-level slash-command registration; type the command in the Moyai conversation.

## Completion and safety

Every goal gets a unique completion marker. A final response must cite successful tool call IDs with a short explanation of how the results verify the goal. A bare “done,” a stale marker, invented call IDs, and failed tool results cannot complete it. A concrete blocker stops continuation and retains the goal as blocked.

This is **evidence-gated self-reporting, not an independent semantic verifier**: the host validates references to actual successful tool calls, while the model must judge whether they satisfy all acceptance criteria. A successful read alone is not a guarantee the objective is achieved. Independent verification and per-criterion structured evidence are future extensions.

Safety stops:

- Explicit Stop, interruption, partial output, provider errors, or missing history.
- Existing administrator iteration/time limits; re-entry does not create a fresh time allowance.
- 25 auto-continuations or one hour per goal budget window.
- Three consecutive responses without tool activity, or three identical responses.
- Tool rounds also check the goal deadline between steps; in-flight tools are never interrupted by the goal deadline.

Worker waits, credential waits, and cooperative machine renewal retain active state and use the existing checkpoint/resumption path. Goal state is written atomically to `/session/goal.json` beside conversation history and travels in the same filesystem checkpoint. It is bound to the run ID so copied child/side-chat files cannot activate a parent's goal. Workspace-recovery warnings disable restoration of possibly stale goal state. No raw tool payloads or credentials are persisted by the goal controller.

## Open-source research

Inspected before implementation:

- [OpenCode ecosystem](https://github.com/anomalyco/opencode/blob/52a6c35825a31a48b58b39253293ed63053f0154/packages/web/src/content/docs/ecosystem.mdx) lists goal functionality as an external plugin, not a core `/goal` command.
- [OpenCode goal plugin](https://github.com/willytop8/OpenCode-goal-plugin/tree/7f0b15fc8dd7792eafa84dcc001174d32284e520): session-scoped state, idle continuation, explicit blockers, evidence-gated completion, bounded loops and paused restart recovery informed this design. Its optional independent auditor and multi-goal sequences are not included here.
- [Hermes programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration) and Moyai's pinned Hermes source: continuation uses the returned `messages`, `completed`, `partial`, `interrupted`, and `pending_steer` contract. Normal model completion is distinct from goal completion.

## Verification

```sh
uv run pytest tests/test_goals.py tests/test_continuation.py tests/test_active_steering.py tests/test_activity.py tests/test_skills.py -q
node --test tests/test_skill_composer.cjs
```

`test_goals.py` executes the production continuation wrapper with a deterministic agent boundary. It verifies a premature final triggers another call with prior history, then successful tool evidence ends the loop. It also covers controls, stop/error/partial handling, stale/invalid evidence, blockers, loop limits, checkpoint recovery/isolation, and native steering receipt behavior. This test does not claim a live model or Modal deployment was exercised.
