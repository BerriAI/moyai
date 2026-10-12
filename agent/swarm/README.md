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

`Agent()` is valid. Optional `name`, `harness: Harness`, and `model` configure a participant. Missing names become unique neutral names; missing runtimes inherit the owning agent's runtime, subject to host compatibility checks. Harness IDs are a typed enum: `HERMES`, `CLAUDE_AGENT_SDK`, `CODEX`, `OPENCODE`, `DEEPAGENTS`, `DEEPAGENTS_TEMPORAL`, `TOOL_LOOP`, and `PI`. Names and the shared task remain text. Definitions are immutable and reject unknown fields, unknown harnesses, blank explicit names, and duplicate names.

`agent.swarm` is an internal extraction boundary for a future SDK. It depends only on the standard library and Pydantic, not Moyai, Temporal, cloud providers or the UI. It is not a separately published package. The host injects an authenticated `SwarmBackend`; Moyai obtains it with `coordinator.swarm(agent_run_id)` during an active turn. The capability is bound to that agent, requester, active turn and execution token, so callers cannot choose another owner or extend its budget.

## Collaboration

Every member receives the complete shared goal and these tools:

- `swarm_board()` reads the actual roster, current lead, assignments and recent public findings.
- `swarm_claim_lead(plan=...)` volunteers to coordinate. The first eligible claim wins atomically; this is self-nomination, not a ranking or consensus election.
- `swarm_assign(member_id=..., task=...)` lets the elected lead queue work in an existing teammate's session.
- `swarm_publish(message=...)` saves findings and wakes the lead to incorporate them. Multiple outstanding updates coalesce into one queued board notification.

The host persists the board, assignments and workflow wakes together. Members finish their turn instead of polling. The lead can be resumed after finishing a turn when another member posts. Queued peer work keeps the group unsettled, so the owner cannot receive a premature final handoff. Failed or cancelled leads are not automatically retried; the failure stays visible for explicit recovery. Peer messages grant no additional permissions and are untrusted reference data.

## Durable results

`start` returns a `SwarmRun` receipt with `id`, `children`, and `checkpoint_required` after durable acceptance, without waiting for models. Identical validated starts in the same owning turn recover the same group; internal keys are scoped to that owner and turn. Save the group ID across turns. The default returns immediately with `checkpoint_required=False`, so the parent can continue independent work or launch another group. Starts still wait for the workspace snapshot and admission; they do not wait for child models.

Use `start(..., wait=True)`, `delegate(..., wait=True)`, or `await group.wait(release=True)` when there is no useful independent work. When the returned `checkpoint_required` is true, return control to the host. Moyai preserves the `moyai_wait_group` marker, checkpoints the owner, releases its sandbox, and resumes it with saved results. Direct Python hosts must implement that checkpoint boundary; the client does not suspend a harness by itself.

```python
async def read_saved_team(backend, group_id):
    result = await AgentSwarm(backend).group(group_id).result()
    for member in result.children:
        print(member.agent_label, member.harness, member.status, member.summary)
    return result
```

`result()` returns partial answers while workers run. `await group.wait(after=result.cursor, seconds=60)` returns when a worker changes, the group settles, or the interval ends. `timed_out` means that read interval ended; it never stops children. The complete child transcript is available through Moyai's `sessions_read` tool. Read completed results before finishing the parent task; Moyai also catches uncollected groups at the parent's final boundary and durably resumes it to gather their results.

`settled` includes failed and cancelled workers; `successful` checks actual outcomes. Default results preserve the handoff even if a user later chats with a worker. Use `result(latest=True)` explicitly for newer work. `group()` only restores a handle; every backend operation rechecks ownership.

`await group.send(child_id, "Check this counterexample.", request_key="counterexample-1")` steers an active child or wakes an idle one. The message is attributed to the parent agent and cannot create new human permissions or goal commands. Repeating the same request key and text reconciles the existing input; use a new key for an intentionally new delivery. A parent follow-up reopens a collected group's result obligation. Human chats alone keep the old handoff frozen.

`await group.cancel(child_ids=[...])` stops selected children; omit `child_ids` to cascade through the whole group. Both retain saved work. `await group.retry(child_ids=[...], instructions="...")` explicitly recovers failed workers; verify ambiguous external actions before retrying. Retries also return immediately unless `wait=True`. Transport errors propagate and uncertain external actions are never automatically replayed.

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
| `planning.py`: balanced runtime selection and continuation prompts | `app/swarms.py`: approved runtime catalog, atomic twenty-member bootstrap, inherited time policy and scheduling |

Product Swarm mode uses `plan_swarm()` to choose twenty neutral members across approved harness/model pairs, without preset roles. It commits the pinned roster, shared goal, scoped uploads and dispatch wakes atomically. Execution capacity controls how many can run at once; the rest wait in the durable queue. Existing swarms retain their saved roster. `plan_team()` remains an explicit role-based planner for advanced callers.

A new host implements `SwarmBackend.call(name, arguments)` for `swarm_start`, `agents_fanout`, `agents_results`, `agents_wait`, `agents_send`, `agents_cancel` and `agents_retry`, plus the collaboration tools exposed to members. It must authenticate ownership, enforce budgets, persist deduplication and implement checkpoint/resume. The client interface alone does not provide durability.

The space UI displays public worker replies and harnesses. The shared chat combines human directions, public replies, and durable board posts with actual author and harness identities; the activity trace remains available. No hidden reasoning is exposed. Tests cover restart recovery, atomic leader claims, peer delivery, coalescing, scope, deadlines and saved result handoffs using the real persistence/workflow code with test sandboxes.

The source defaults to 100 workers per group through `MAX_PARALLEL_AGENTS`; operators can choose any positive value. Admission still enforces configured runtime capacity and inherited time budgets. Prompts, mission text, board posts, item lists and returned child answers have no fixed character or list-length ceilings. Large tool arguments use authenticated multipart transport across the HTTP envelope boundary.

This behavior requires schema revision 10 and role protocol revision 5. Older workers treat agent messages as human input and impose fixed swarm deadlines, so use the coordinated release path in [rolling deployments](../../docs/rolling-deploy.md); do not mix these protocols.
