# Moyai agent

Start with `agent.py`. It owns the conversation, prompt, goals, steering, and saved context. Production and evaluations call the same `run()` function with a prepared workspace, session directory, broker, and event callback.

```text
agent.py     Agent setup and conversation lifecycle
prompts/     System instructions and conditional Slack/delegation guidance
tools/       Tool implementations and descriptions
harnesses/   Existing Codex, Claude, Hermes, and LiteLLM adapters
swarm/       Agent-owned team planning, typed delegation API and backend contract
skills/      Bundled task procedures
```

The existing context, goal, and event helpers sit beside `agent.py`. Sandbox provisioning, repository checkout, project services, browser processes, credential transport, and artifact collection belong in `sandbox/`. The product API, UI, and integrations stay in `app/`.

An agent can own a swarm, and its workers can own further teams under the same inherited limits. See [the swarm Python interface](swarm/README.md) for submission, durable handoff, results and backend integration. The ten-agent product mode uses this package's planning and prompt policy; broker tools and Python callers share its request contracts.

To improve behavior, edit a prompt or tool, then run the representative cases in `evals/`. Each evaluation records the source revision. Harness implementations remain responsible for their native model/tool loops; this package supplies the shared Moyai behavior around them.
