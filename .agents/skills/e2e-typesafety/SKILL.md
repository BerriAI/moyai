---
name: e2e-typesafety
description: Keep the Python API and the native/web frontend type-safe end to end (Pydantic → OpenAPI → hey-api generated client). Use whenever adding or changing a FastAPI route, a request/response payload, or a frontend call to /api.
---

# End-to-end type safety

**Definition.** Renaming, removing or retyping a field in a Pydantic model must
fail `npm run typecheck` at every frontend call site that depends on it — before
review, before a user sees `undefined`. If a backend payload change can ship with
a green typecheck, the API is not type-safe, no matter how many TypeScript types
exist.

Python is the single source of truth. TypeScript never describes a backend
payload by hand.

```
Pydantic model ─▶ FastAPI route (return annotation) ─▶ app.openapi()
   ─▶ native/web/generated/openapi.json (committed)
   ─▶ @hey-api/openapi-ts ─▶ native/web/generated/api/ (committed, never hand-edited)
   ─▶ native/web imports types + SDK functions
```

## What this replaces

Today most routes return `dict`s with no annotation, and the frontend calls
`api<SomeType>(path)`, where `SomeType` is a hand-written guess. That `<T>` is a
cast: it compiles whatever the server actually sends. Treat both as legacy:

- Do not add new unannotated JSON routes or new `api<T>()`/`apiRequest<T>()` calls.
- When you touch a legacy route or call site, migrate it (see below) in the same change.

## Backend rules

1. **Every JSON route returns a Pydantic model**, declared as the handler's
   return annotation (FastAPI uses it as `response_model`). No `dict`, `Any`,
   bare `JSONResponse`, or unannotated handlers for JSON payloads. Lists are
   `list[Model]`; maps are `dict[str, Model]`.
2. **Request bodies are Pydantic models**, never `dict` or `await request.json()`.
3. **Make the domain visible in the schema.** Use `Literal[...]`/`StrEnum` for
   closed sets (they become TS unions), discriminated unions with
   `Field(discriminator=...)` for variants, and `Query(...)`/`Path(...)` types
   for parameters. A `str` that is really one of four values is a type hole.
4. **Be deliberate about optional vs nullable.** `x: str | None` (required,
   nullable) and `x: str | None = None` (optional) generate different TS. Pick
   the one that matches what the server actually does.
5. **Stable, unique operation IDs.** They become SDK function names. Set
   `operation_id=` explicitly in camelCase verb + path noun
   (`@router.get("/api/history/sessions", operation_id="listHistorySessions")`),
   never the FastAPI default (`sessions_api_history_sessions_get`). The dump
   fails on duplicates.
6. **Defaults are required on output.** Pydantic marks a field with a default
   as optional in the response schema, so TS gets `field?: T`. Every response
   model (and any model shared between input and output) sets
   `model_config = ConfigDict(json_schema_serialization_defaults_required=True)`;
   FastAPI then emits `FooInput`/`FooOutput` where the two differ.
7. **Name models for the whole API.** Schema names are global in
   `components.schemas`; two `Session` models in different modules become
   `moyai__x__Session`. Reuse the existing model when the payload is the same;
   otherwise name it distinctly (`HistorySession`).
8. **Declare error shapes.** Errors use the shared error model
   (`{"detail": str}` / the 422 shape from `safe_validation` in
   `native/src/moyai/api.py`) via `responses={...}` so the client's error type is
   generated too.
9. **Response models are also a security boundary.** FastAPI filters output to
   the declared model, so internal fields can't leak by accident. Don't bypass
   it with `JSONResponse(content=...)`.

**Out of the OpenAPI path** (file downloads, streams, SSE, websockets, noVNC):
these may return `Response`/`StreamingResponse`, but any JSON *message* they
carry is still a Pydantic model, and the dump script adds those models to
`components.schemas` so the frontend imports their generated types too.

## Generation

- `npm run api:generate` — `python -m moyai.openapi_dump` (builds the app
  in-process, no server) writes `native/web/generated/openapi.json`, then
  `openapi-ts` (config: `openapi-ts.config.ts`) writes `native/web/generated/api/`.
- `npm run api:check` — regenerates and fails if `native/web/generated` changed.
  Runs in CI (`.github/workflows/frontend.yml`) before `typecheck`.

`openapi_dump.py` builds the app with a synthetic product configuration so
**every optional router is mounted** (history, continuations, files, settings,
automations, Slack…). If you add a router behind a new runtime condition, make
sure the dump still mounts it, or its types silently disappear.

`native/web/api-client.ts` points the generated client at `send()` in
`native/web/api.ts`, the same transport as legacy `api<T>`: CSRF headers, a
single attempt (never replay a write), `ApiError` with friendly messages, and
the `moyai:signed-out` event on 401. Change transport behavior there, once.

Generated SDK functions **throw `ApiError`** and resolve to `{ data, request,
response }`:

```ts
import { getMe } from "./generated/api"

const { data: me } = await getMe({ signal })
```

`/api/me` (`native/src/moyai/api.py`, `api_views.py`, `native/web/main.tsx`) is
the reference migration.

## Frontend rules

1. **Never hand-write a type for a backend payload.** Import it from
   `native/web/generated/api`. If the type you need isn't generated, the backend
   is missing a model — fix it there.
2. **Call generated SDK functions**, not `fetch`/`api<T>()` with a URL string.
   Paths, params, bodies and responses are then all checked.
3. **No `as` casts on API data.** A cast on a response is a type-safety bug,
   just like `api<T>`.
4. **UI-only types derive from generated ones** (`Pick`, `Omit`, `&`), so a
   backend rename still propagates.
5. **Never edit `native/web/generated/`.** Regenerate.

## Migrating a legacy route

1. Write the Pydantic response (and request) model next to the route (for
   `api.py`, in `api_views.py`); annotate the handler; give it an operation ID.
   Build the model from what the handler returns today, field by field, and
   check how the frontend reads it: a key that is sometimes absent is optional,
   a key that can be `null` is nullable.
2. Add a backend test asserting the real response validates against the model.
3. `npm run api:generate`.
4. Replace the `api<T>()` call with the SDK function; delete the hand-written TS
   type and fix whatever `npm run typecheck` now reports — those errors are
   drift that already existed.
5. Commit models, generated files and call sites together.

## Done means

- `npm run api:check` and `npm run typecheck` pass.
- The changed routes appear in `openapi.json` with concrete schemas (no `{}` /
  `additionalProperties: true` where a model belongs).
- No new hand-written payload types, `api<T>` calls or casts in `native/web`.
