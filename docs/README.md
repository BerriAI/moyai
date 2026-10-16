# Documentation

[Project overview and local installation](../README.md)

| Guide | Purpose |
| --- | --- |
| [Using Moyai](usage.md) | Chat, computer, files, Settings, connections and Slack |
| [Architecture](architecture.md) | Execution ownership, service roles and storage boundaries |
| [Docker installation](../deploy/docker/README.md) | Versioned standalone or split services and upgrades |
| [Production deployment](production-deploy.md) | Prepare and upgrade the existing four-role product |
| [Deployment contract](pydantic-deployment.md) | Configuration, schema receipts, drain and release ordering |
| [Authentication](authentication.md) | Local accounts and existing Google/Cloudflare identity |
| [Cloudflare Access](cloudflare-access.md) | Preserve the existing edge and origin identity boundary |
| [Automations](pydantic-automations.md) | Canonical definitions, adoption and admission fences |
| [Workspace recovery](pydantic-workspace-recovery.md) | Checkpoint, cold restore and uncertain effects |
| [Runtime performance](pydantic-performance.md) | Activity queues, capacity and computer startup |
| [PostgreSQL runtime](postgres-runtime.md) | Canonical and native schema ownership |
| [Schema migrations](schema-migrations.md) | Receipts, offline migration and compatibility |
| [PostgreSQL migration](postgres-migration.md) | Verified historical SQLite transfer |
| [Object storage](object-storage.md) | Historical payload migration and backup requirements |
| [UI components](shadcn-ui.md) | Shared controls, frontend build and browser checks |
| [Verification](verification.md) | Local and CI checks and their limits |
| [Consolidation scope](consolidation.md) | Preserved functionality and removed runtime families |

Older designs and rollout reports remain in Git history. They describe their
recorded revision and are not instructions for starting the current runtime.
