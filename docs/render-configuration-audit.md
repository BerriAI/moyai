# Configuration ownership audit

Repository names were used as a second allowlist: deployment configuration restricted which repositories a shared GitHub App could use, independently of its installation. The model gateway does not need that list. The extra restriction was reasonable; keeping it in environment variables and comparing mutable names made renames and repository onboarding require a restart.

This change moves the selected repository IDs into the encrypted Moyai connection and keeps current names/verified aliases in a database catalog. Administrators select repositories in Connections. GitHub’s installation remains the outer permission boundary. See [GitHub setup and migration](integrations.md#shared-organization-github).

| Setting | Finding | Treatment |
| --- | --- | --- |
| `GITHUB_REPOSITORY`, `GITHUB_REPOSITORIES` | Product access selection duplicated in deployment settings, connector credentials and sandbox spec; names broke on rename. | Removed as runtime settings. Selection, routing, token scope and saved references use GitHub IDs. |
| `LITELLM_TRACE_ENDPOINT` | The generic Render blueprint named one particular development gateway. | Changed to deployment-supplied configuration. Tracing is optional; inference routing stays separate. |
| `BRAINTRUST_PARENT` | The blueprint contained one customer's literal project UUID. | Changed to deployment-supplied configuration. |
| `SLACK_SESSION_USERS` | A product access policy still maintained as an environment allowlist of Slack IDs. | Candidate for an audited organization settings page. Left intact so this migration does not change who can start sessions. |
| `GOOGLE_ALLOWED_DOMAINS`, `GOOGLE_ADMIN_EMAILS` | Login and bootstrap administrator policy; the code defaults the domain to `berri.ai`. | Organization-specific setup must set these explicitly. A future settings migration needs an administrator recovery path. No access restrictions changed here. |
| `ORGANIZATION_NAME` | Bootstrap default only; Moyai already stores the organization name in its database. | Already editable in Connections without deployment. Keep the environment value as an initial default. |
| `AUTO_PREPARE_REPOSITORIES`, default harness/model | Workspace defaults are deployment-configured. Models/harnesses also have session-level choices. | Candidates for organization settings if administrators need to change defaults without deployment. They are not repository authorization. |
| LiteLLM and custom environment templates | Source templates include example repository names; auto-discovery recognizes LiteLLM to apply its setup recipe. | Templates are editable starting points, not an access list. Saved recipes are bound to IDs. |
| Modal, Temporal, gateway addresses/keys, encryption/session keys, data disk, timeouts/concurrency | Infrastructure, secrets and capacity controls. | Appropriate deployment configuration. Do not move secret values into a public browser setting. |
| `LANGSMITH_PROJECT=moyai`, standard vendor endpoints, tracing environment | Telemetry labels and documented service defaults. | Reasonable defaults with per-deployment overrides; unrelated to repository names or authorization. |
| `AGENT_HARNESS` | A live Settings field, not a dead environment variable. | Retained as the deployment's default harness. |
| `RENDER_MIGRATION_STAGE`, `BOOTSTRAP_MODAL_VOLUME` | Real migration controls used by `render_start.py`. | Retain documented cutover behavior. Leave bootstrap volume empty after a completed import; do not remove staging protection during unrelated work. |

The audit covers the checked-in configuration and code paths. It does not claim that every live Render variable or secret was audited. Editing the blueprint does not remove existing values from an already-running service.

## Rollout

1. Back up the durable database and keep the existing `ENCRYPTION_KEY`.
2. Deploy this code. The first GitHub use/check migrates the saved selection without expanding it. Migration verifies repository IDs and the numeric organization owner before saving; a concurrent disconnect/reconnect wins.
3. Open Connections, check GitHub, and confirm the selected repositories. Test read/checkout and a normal PR flow. Old in-progress publication requests without a confirmed receipt require manual inspection before using a new request key.
4. Remove the now-ignored `GITHUB_REPOSITORY` / `GITHUB_REPOSITORIES` variables. Keep the intended live telemetry destinations explicitly configured. No gateway repository setting is required.

The initial conversion can only verify names that the old database actually saved; it cannot reconstruct a historic ID if a name was already deleted or reassigned before migration. Inspect the selection at rollout. Once pinned, renames and name reuse cannot retarget saved work.
