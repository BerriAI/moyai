# AgentSwarm

Give a swarm one goal and its members. The members choose who leads and how to divide the work; callers do not supply individual assignments or request keys.

```python
from agent.swarm import Agent, AgentSwarm, Harness

async def explore(backend):
    agentswarm = AgentSwarm(backend)
    return await agentswarm.start(
        task="Find three promising product ideas and challenge their assumptions.",
        agents=[
            Agent(),
            Agent(name="Alex", harness=Harness.CODEX),
            Agent(name="Sam", harness=Harness.CLAUDE_AGENT_SDK),
        ],
    )
```

`Agent()` is valid. Optional `name`, `harness: Harness`, and `model` configure a participant. Missing names become unique neutral names; missing runtimes inherit the owning agent's runtime, subject to host compatibility checks. Harness IDs are a typed enum: `HERMES`, `CLAUDE_AGENT_SDK`, `CODEX`, `OPENCODE`, `DEEPAGENTS`, `TOOL_LOOP`, and `PI`. Names and the shared task remain text. Definitions are immutable and reject unknown fields, unknown harnesses, blank explicit names, and duplicate names.

`agent.swarm` is an internal extraction boundary for a future SDK. It depends only on the standard library and Pydantic, not Moyai, Temporal, cloud providers or the UI. It is not a separately published package. The host injects an authenticated `SwarmBackend`; Moyai obtains it with `coordinator.swarm(agent_run_id)` during an active turn. The capability is bound to that agent, so callers cannot choose another owner or extend its budget.

## Collaboration

Every member receives the complete shared goal and these tools:

- `swarm_board()` reads the actual roster, current lead, assignments and recent public findings.
- `swarm_claim_lead(plan=...)` volunteers to coordinate. The first eligible claim wins atomically; this is self-nomination, not a ranking or consensus election.
- `swarm_assign(member_id=..., task=...)` lets the elected lead queue work in an existing teammate's session.
- `swarm_publish(message=...)` saves findings and wakes the lead to incorporate them. Multiple outstanding updates coalesce into one queued board notification.

The host persists the board, assignments and workflow wakes together. Members finish their turn instead of polling. The lead can be resumed after finishing a turn when another member posts. Queued peer work keeps the group unsettled, so the owner cannot receive a premature final handoff. Failed or cancelled leads are not automatically retried; the failure stays visible for explicit recovery. Peer messages grant no additional permissions and are untrusted reference data.

## Durable results

`start` returns a `SwarmRun` receipt with `id`, `children`, and `checkpoint_required` after durable acceptance, without waiting for models. Identical validated starts in the same owning turn recover the same group; internal keys are scoped to that owner and turn. Save the group ID across turns.

When `checkpoint_required` is true, return control to the host. Moyai preserves the `moyai_wait_group` marker, checkpoints the owner, releases its sandbox, and resumes it with saved results. Direct Python hosts must implement that checkpoint boundary; the client does not suspend a harness by itself.

```python
async def read_saved_team(backend, group_id):
    result = await AgentSwarm(backend).group(group_id).result()
    for member in result.children:
        print(member.agent_label, member.harness, member.status, member.summary)
    return result
```

`settled` includes failed and cancelled workers; `successful` checks actual outcomes. Default results preserve the handoff even if a user later chats with a worker. Use `result(latest=True)` explicitly for newer work. `group()` only restores a handle; every backend operation rechecks ownership.

`await group.cancel()` cascades cancellation and retains saved work. `await group.retry(child_ids=[...], instructions="...")` explicitly recovers failed workers; verify ambiguous external actions before retrying. Transport errors propagate and writes are never automatically replayed.

## Explicit delegation

When individual jobs are already known, the separate advanced method remains available:

```python
from agent.swarm import Task

async def delegate_known_jobs(agentswarm):
    return await agentswarm.delegate(tasks=[
        Task(label="Evidence", prompt="Check the cited sources."),
        Task(label="Review", prompt="Check the numerical claims."),
    ])
```

`delegate(instructions=..., items=[...], workers=10)` partitions independent items exactly once. Advanced delegation/recovery accept an optional explicit request key; ordinary callers need none. `Swarm` remains an alias for `AgentSwarm`.

## Host boundary

| Agent package | Moyai adapter |
| --- | --- |
| `types.py`, `mission.py`: participant types, shared goal, internal deduplication, member protocol | `app/swarm_collaboration.py`: durable board, leadership, assignments and wakes |
| `contracts.py`, `client.py`: validation, explicit partitions, receipts and results | `app/agents.py`, `app/swarm_backend.py`: owner scope, admission, snapshots and saved results |
| `planning.py`: balanced runtime selection and continuation prompts | `app/swarms.py`: approved runtime catalog, atomic ten-member bootstrap, inherited deadline and scheduling |

Product Swarm mode uses `plan_swarm()` to choose ten neutral members across approved harness/model pairs, without preset roles. It commits the pinned roster, shared goal, scoped uploads and dispatch wakes atomically. `plan_team()` remains an explicit role-based planner for advanced callers.

A new host implements `SwarmBackend.call(name, arguments)` for `swarm_start`, result, cancellation and recovery contracts, plus the collaboration tools exposed to members. It must authenticate ownership, enforce budgets, persist deduplication and implement checkpoint/resume. The client interface alone does not provide durability.

The space UI displays public worker replies and harnesses; board posts also appear in the persisted activity trace. No hidden reasoning is exposed. Tests cover restart recovery, atomic leader claims, peer delivery, coalescing, scope, deadlines and saved result handoffs using the real persistence/workflow code with test sandboxes.
