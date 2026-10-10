---
name: delegation
description: Split independent work across real child agents, prepare their environment and integrate verified results.
---

Delegate when the current user authorizes independent child work. Use the actual agent tools, never simulated model calls described as agents. Give each assignment the original objective, its scoped task, constraints, source references, expected output/evidence and relevant authorized capabilities. Private memory or credentials must not be copied into unauthorized child contexts.

Before fan-out, inspect the selected versioned environment, repository identity/ref, runtimes, package manager, services and required access. Reuse a prepared environment for common prerequisites; do not launch many workers into the same known missing dependency. Independent tasks can proceed when a dependency affects only part of the work. Report the verified environment identity, not an assumed recipe outcome.

Use the returned child handles. Continue independent parent work when the runtime permits it; explicitly wait for dependencies rather than treating a group as all-or-nothing. Inspect partial progress and finished children, send scoped corrections, and retry failed assignments without repeating successful siblings. Keep separate workspaces or branches for independent edits. Collect exact artifact revisions/commits and validate the combined result.

Group settlement is not success. Account for missing, failed and still-running children before finishing the root task. Stopping the root must settle dependent work through the available cancellation controls. Follow the runtime's actual capability and recovery receipts; do not claim concurrency or child completion without evidence.
