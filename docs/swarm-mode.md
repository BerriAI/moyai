# Swarm mode

Choose **Swarm**, describe a task, and set a maximum duration. Moyai asks the coordinator to delegate complementary work to real child agents, gather their outputs, and produce a useful result. Available harnesses and models can differ between workers. The activity view shows actual launches, handoffs, tools, and messages; it does not invent an agent team before work starts.

Swarm mode requires the existing cloud sandbox setup and Temporal execution. It uses Moyai's durable sessions, saved conversation/workspace checkpoints, delegation tools, and permission boundaries. It is not an independent background loop or a Hermes `/goal` command.

After a confirmed, checkpointed answer, the host waits 30 seconds using a durable timer, then schedules another round while budget remains. Automatically generated messages are marked as swarm continuations. A human message takes priority over a continuation that has not started. Agent context retains the original task, prior results, and human directions.

The duration is a wall-clock deadline from creation, including queuing, sandbox preparation, delegated work, and pauses. Refreshing the page or replacing a worker does not reset it. The deadline also applies to descendants; the host cancels unfinished work when it expires. There is a 25-round maximum and a guard against repeatedly returning an identical answer.

**Pause** cancels current execution and preserves saved work. **Resume** is explicit and only available after cleanup while time remains; it keeps the original deadline. **Stop** permanently ends this mission and cascades to delegated agents. Start a new mission for a new time budget. Failed, interrupted, or uncertain execution blocks automatic continuation; review saved results and verify uncertain external actions before explicitly resuming. Normal app approvals still apply.

The API accepts `swarm: {"budget_seconds": 300}` on `POST /api/runs` (60–86,400 seconds). Run details include `swarm.status`, `budget_seconds`, `ends_at`, `round`, and `reason`. Controls are `POST /api/runs/{id}/swarm/pause`, `POST /api/runs/{id}/swarm/resume`, and the existing `POST /api/runs/{id}/cancel`. They require normal authentication and mutation protection. Creation, mission intent, the initial message, and its durable dispatch wake commit atomically; continuation messages, round advancement, and their dispatch wake do likewise.

Schema revision 3 adds `swarm_missions` and pinned per-worker runtime assignments. For deployments using explicit PostgreSQL migrations, stop runtime owners and run the existing offline migration before starting this version. This feature does not deploy or migrate an existing production installation automatically.

## Interface preview

These are actual browser captures of the production frontend against synthetic local fixtures. They demonstrate the interface, not live model execution. The creation comparison uses the same sample data and a 1440 × 900 viewport.

| Before | Swarm mode |
| --- | --- |
| ![Original new session](assets/swarm-mode-before.jpg) | ![Swarm mission and duration](assets/swarm-mode-after.jpg) |

![Swarm space with saved agent identities](assets/swarm-mode-space.jpg)

[Mobile view at 320px](assets/swarm-mode-mobile.jpg). The interface was also checked at 768px, including draft preservation between Chat and Space, child conversations and model identity, duration selection, Pause/Resume/Stop, keyboard focus, and blocked/expired message controls. Reduced-motion behavior is covered by the canvas implementation; a screen-reader audit was not performed.

To inspect the same sample locally, run `npm run preview -- --port 8840` and open `http://127.0.0.1:8840/?fixture=swarm#run=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`. Alternate fixtures are `swarm-paused`, `swarm-expired`, and `swarm-error`.
