# Repository guidance

For frontend work in `native/web` or `frontend`, read [.agents/skills/moyai-ui/SKILL.md](.agents/skills/moyai-ui/SKILL.md) and [the component contract](docs/shadcn-ui.md) before editing. Use the shared shadcn components and verify affected pages in the browser. This applies to UI work, not unrelated backend changes.

For any change to a FastAPI route, a request/response payload, or a frontend call to `/api`, read [.agents/skills/e2e-typesafety/SKILL.md](.agents/skills/e2e-typesafety/SKILL.md) first. Payload types come from Pydantic via OpenAPI and the generated hey-api client; never hand-write them in TypeScript.
