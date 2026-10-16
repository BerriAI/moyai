# Consolidation scope

This change makes the Pydantic product the sole executable application while
preserving the behavior present in the native product at main commit
[`9b6f460`](https://github.com/BerriAI/moyai/tree/9b6f460fe4785f60059fe4d0ffc78a7bb94b599e).
It also incorporates main through `cf1844f`, including the versioned Docker installer, full-row sidebar
links, Midnight violet palette and parallel CI execution. It does not claim parity with every retired engine feature.

## Size

Application source falls from **102,436 to 61,168 lines (40.3%)** against `cf1844f`.
This counts Python, JavaScript/TypeScript, CSS and HTML in the application trees
and root startup files, including comments/blank lines. It excludes generated
bundles, vendored code, generated protocol code, tests, scripts and documentation.
The latest Docker installer remains included in both sides of the comparison.

## Removed

- Legacy agent loops, Deep Agents integration, harness execution adapters and
  their old sandbox implementations (`agent/`, `sandbox/`, legacy `app` executors).
- The legacy HTML/controller frontend and its React template bridge. Native
  assets and pure analytics/automation helpers now belong to the native frontend.
- The second Python lock/environment and image startup switch. Local and
  existing-product configurations use one installation.
- Legacy-only tests, evaluation harnesses, deployment recipes and documentation.
  Their source and evidence remain at the historical revision above.

## Preserved

- Pydantic AI/Harness, Temporal replay and Modal execution; web/Slack surfaces,
  desktop, attachments, settings, native delegation and ordinary side chats.
- Canonical identities, permissions, history/continuation, credential metadata,
  accounting, memory and supported automation adoption.
- Historical archive readers, object formats, encryption keys, schema manifests
  and migration receipts. Canonical schema revision 21 and its digest are unchanged.
- API/coordinator/broker/worker ownership, release drain, current-authority checks,
  uncertain-effect receipts and provider billing boundaries.
- Historical source-image probes needed for a guarded upgrade. They can inspect
  the former `native/.venv` image layout without restoring an executable old engine
  to the current package.

## Deliberately retained limits

Interactive child-agent chats, full legacy swarm/goal semantics, canonical skill
save/import conversion, skill learning and desktop recording remain deferred.
The consolidation does not silently add them or describe them as supported.
Standalone local accounts and existing-product identity remain separate valid
configurations because merging them would risk replacing an existing user directory.

Tests cover controlled-model Temporal execution and replay, real PostgreSQL,
packaging, Docker persistence, browser interactions and simulated release control
planes. These do not establish a production deployment or every live provider
operation. See [verification](verification.md) and the PR for recorded results.
