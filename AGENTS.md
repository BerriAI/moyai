# Repository guidance

For frontend work in `native/web` or `frontend`, read [.agents/skills/moyai-ui/SKILL.md](.agents/skills/moyai-ui/SKILL.md) and [the component contract](docs/shadcn-ui.md) before editing. Use the shared shadcn components and verify affected pages in the browser. This applies to UI work, not unrelated backend changes.

For any change to a FastAPI route, a request/response payload, or a frontend call to `/api`, read [.agents/skills/e2e-typesafety/SKILL.md](.agents/skills/e2e-typesafety/SKILL.md) first. Payload types come from Pydantic via OpenAPI and the generated hey-api client; never hand-write them in TypeScript.

For native schema versions, register both `product_schema.MIGRATIONS` and the frozen expansion in `native_online_schema.EXPANSIONS`. Preserve historical plan identities, append future test fixtures after the current version, and run `test_native_online_schema.py`, `test_native_additive_schema.py`, and `test_product_schema_versions.py` with disposable PostgreSQL before pushing.
