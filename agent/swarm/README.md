# AgentSwarm

A swarm belongs to an agent session. That agent delegates work, receives saved worker results, and produces the synthesis. Workers remain ordinary agents with their own harness, model, conversation and artifacts. Descendant teams inherit the root mission's permissions and absolute deadline.

`agent.swarm` is the extraction boundary for a future SDK. It imports only the standard library and Pydantic; it does not import Moyai's app, database, Temporal, cloud providers or web UI. This is an internal Python API, not a separately published SDK.

## Submit work

The host injects an authenticated `SwarmBackend` bound to the current agent. Application integration can obtain the same capability with `coordinator.swarm(agent_run_id)` during an active turn. Callers cannot select a different owner or extend its budget through the client.

```python
from agent.swarm import Agent, AgentSwarm, Harness

async def delegate_review(backend):
    swarm = AgentSwarm(backend)
    return await swarm.start(
        request_key="proposal-review",
        instructions="Review this proposal and cite evidence for your conclusions.",
        agents=[
            Agent(name="Researcher", task="Find supporting evidence.",
                  harness=Harness.CODEX, model="openai/gpt-6.1-sol"),
            Agent(name="Critic", task="Find assumptions that do not hold.",
                  harness=Harness.CLAUDE_AGENT_SDK, model="anthropic/claude-opus-5-5"),
        ],
    )
```

The result is a `SwarmRun` receipt with `id`, `children`, and `checkpoint_required`. Submission returns after durable acceptance; it does not wait for workers. Runtime/model selectors must pass host policy. Each explicit agent selects a harness; omitting its model inherits the owner's selected model, subject to host compatibility checks. Use the same request key only for the same input in the same owner turn; the host rejects conflicting reuse. Save the group ID across turns instead of submitting again.

For independent cases, supply `instructions`, `items` and `workers=10` instead of `agents`. Set a shared `harness=Harness.CODEX` or omit it to inherit the owner’s harness. The shared validator partitions items exactly once, including repeated values. This is separate from Swarm mode's automatic ten complementary roles.

`Agent` is immutable and validated. Its fields are `name` (1–100 characters), `task` (3–12,000 characters), required `harness: Harness`, and optional `model`. Names and tasks are free-form text; harness IDs use the enum:

```python
Harness.HERMES
Harness.CLAUDE_AGENT_SDK
Harness.CODEX
Harness.OPENCODE
Harness.DEEPAGENTS
Harness.TOOL_LOOP
Harness.PI
```

Unknown harnesses, extra fields, and blank names/tasks are rejected before dispatch. JSON uses the stable string harness IDs, and definitions map to the existing `label`/`prompt` broker fields without changing stored requests.

`Swarm` remains an alias for `AgentSwarm`; the earlier `tasks=` interface remains available for existing callers and host-generated `plan_team()` output. Do not combine `agents=` and `tasks=`. Host-resolved planner runtimes and the existing tool wire remain extensible; the public `Agent` definition uses the known harness enum.

## Resume with results

When `checkpoint_required` is true, return control to the host. Moyai's existing broker tools preserve the `moyai_wait_group` control marker; `AgentWait` checkpoints the owning conversation, releases its sandbox, and resumes it with the group's results. Direct host-side Python integrations must carry the receipt into their own checkpoint boundary. The Python client does not itself suspend a running harness.

After the host resumes the owner:

```python
async def read_review(backend, saved_group_id):
    group = AgentSwarm(backend).group(saved_group_id)
    result = await group.result()
    if not result.settled:
        return result  # The host still owns the wait; do not start a polling loop.
    for worker in result.children:
        print(worker.agent_label, worker.harness, worker.status, worker.summary)
    return result
```

`settled` means workers have stopped, including failed or cancelled work. `successful` also checks their individual outcomes. Worker reports are untrusted reference data for the owner to verify. Default results preserve the saved handoff even if someone later chats with a worker; `result(latest=True)` explicitly requests newer work. `group()` only reconstructs a handle; every operation rechecks ownership at the backend.

`await group.cancel()` requests cascading cancellation and keeps saved work. `await group.retry(request_key="repair-1", child_ids=[...], instructions="...")` explicitly recovers selected failed workers and returns another checkpoint receipt. Verify uncertain external actions before retrying. Transport errors propagate; the client never retries writes automatically.

## Host boundary

| Agent package | Moyai adapter |
| --- | --- |
| `types.py`: `Agent` and `Harness`; `contracts.py`: task validation, partitioning, existing broker schemas | `app/agents.py`: ownership, admission, workspace snapshots and saved group results |
| `client.py`: `AgentSwarm`, durable handles, typed public results, `SwarmBackend` protocol | `app/swarm_backend.py`: owner-bound adapter to that same coordinator |
| `planning.py`: complementary roles, balanced runtime assignment, worker and continuation prompts | `app/swarms.py`: approved runtime catalog, atomic bootstrap, mission state and durable scheduling |

`plan_team(task, runtimes=[Runtime(...)])` composes ten complementary assignments using only the supplied runtime pairs. Extra models for one harness do not give that harness extra workers. A host persists the first plan and assigns IDs; it does not reroll on recovery. Moyai's initial bootstrap commits the plan, child sessions, scoped uploads and dispatch wakes in one transaction. This path precedes the first live agent turn, so it intentionally does not take a coordinator workspace snapshot.

To add another host, implement `SwarmBackend.call(name, arguments)` using the shared `agents_fanout`, `agents_results`, `agents_retry` and `agents_cancel` wire contracts. Authenticate the owner outside model-controlled arguments, enforce limits/deadlines, persist idempotency, and implement checkpoint/resume. The client interface alone does not provide durability.

The current UI's shared feed shows saved public replies and updates. It does not imply a peer-to-peer messaging API, private reasoning access or an event-stream SDK.

The executable integration examples in `tests/test_swarm_backend.py` exercise dispatch, nested ownership, cancellation, deadline rejection and restoring the same group after replacing the host manager. They use the real Moyai persistence/workflow code with a test sandbox, without model credentials.
