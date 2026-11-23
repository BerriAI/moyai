# Conversation state

This module owns the frontend's conversation reads, live transcript and pending
session edits. Callers import the specific file they use; there is no package
barrel that eagerly loads every hook.

| Interface | Responsibility |
| --- | --- |
| `use-session-inventory.ts` | Authorized sidebar/home inventory, paging, folders and connections |
| `use-conversation-lifecycle.ts` | Mounted conversation history, live cursor, access invalidation and accepted receipts |
| `use-session-updates.ts`, `session-updates.ts` | Scoped, ordered edits shared across independently refreshed views |
| `use-work-progress.ts` | One shared progress read per execution |
| `conversation-state.ts`, `live-transcript.ts`, `work-timeline.ts` | Pure reconciliation and transcript/progress presentation |
| `session-list.ts` | Existing list-row types and source-aware identity helpers used by state and renderers |

The mounted session and authority scope still own cancellation and invalidation.
Saved history remains authoritative. Refresh failures never replay a write.
No polling interval, request, event cursor or pending-edit policy changes with
the package location.

Conversation state may depend on React, the generated client, shared transport
(`api.ts`, `api-client.ts`), visibility polling and attachment helpers. It cannot
import application composition, UI renderers or another feature. Shared transport,
polling and uploads cannot import conversation state. `tests/conversations` checks
these rules with TypeScript module resolution, including type imports, re-exports
and literal dynamic imports.

`main.tsx` composes these interfaces with the existing home, chat, sidebar, action
menu and progress renderers. Shared controls remain in `frontend/components/ui`.
Generated payload types and transport stay in `native/web/generated` and
`native/web/api*.ts`; do not copy them into this directory.

Unit tests live in `tests/conversations` and run with `npm test`. Browser scenarios
remain in `tests/browser`, where they exercise the real React composition with
controlled APIs; they are supplementary to actual-app E2E. TypeScript includes
nested source files, and the Python API contract check scans `native/web`
recursively. `npm run build` emits the same packaged static tree.
