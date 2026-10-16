---
name: moyai-ui
description: Build or refine Moyai's native React frontend using its established navigation, typography, forms and interaction patterns. Use for native/web and shared frontend changes or UI reviews, not backend-only work.
---

# Moyai UI

Preserve the deep-space workspace: near-black navy surfaces, icy-blue actions,
ivory text, local Geist typography and restrained glass panels. The home screen
uses the deterministic constellation and Moyai sculpture; chat and Settings stay
quiet for sustained work. Inspect the current screens before changing them.

## Sources and controls

- `native/web`: application, settings, panel and sidebar React components/styles.
- `frontend/components/ui`: shared shadcn/Radix controls. Use direct components,
  including accessible Dialog, Select, Popover and form primitives.
- `frontend/theme.css`: semantic color roles, shared sizing and portal geometry.
- `native/web/assets` and `public`: local visual assets, fonts and sculpture model.
- [Component contract](../../../docs/shadcn-ui.md): build and verification workflow.

Keep API/state logic in the existing hooks and adapters. Avoid alternate renderers,
feature-specific themes or nearly identical token overrides. Build with `npm run build`; generated `native/src/moyai/static` is ignored by Git
and packaged at build time.

## Layout and interaction

Use one page heading, clear purpose text, compact rows and consistent leading
edges. Reuse the existing spacing scale and content measures. Search fills its
row while filters/actions retain their intrinsic width; on narrow screens it
gets its own row. Preserve complete labels or provide explicit expansion.

Use one primary action per context. Colors communicate status alongside text,
not alone. Dialogs need names, labels, errors, cancellation and contained scrolling.
Tables scroll within their own labelled, keyboard-focusable container. Keep normal
text contrast at 4.5:1 and meaningful boundaries/focus at 3:1.

Preserve API contracts, capabilities, personal/shared scopes, revisions and all
existing actions. Reject stale async responses after route, actor or scope changes.
Refreshes preserve active drafts, filters and useful focus. A route change updates
its title and moves focus appropriately. Loading must not leave another page's
controls under a new title. Permission loss clears protected state promptly.

Provide visible keyboard focus and accessible control names. Destructive actions
explain their effect and support cancellation. Use 16px mobile inputs, wrapping
action groups and flexible grid columns. Preserve long-identifier wrapping.
Respect reduced motion and avoid `transition: all`. Show actionable empty/error
states, with clear filters or retry where applicable.

## Verify

Run `npm run typecheck`, relevant `npm test` cases, `npm run build` and
`npm run test:ui`. The browser fixtures serve the actual React app against synthetic
APIs; they do not prove provider authorization or production execution.

Inspect affected screens at 1440px, 768px and 320px. Include the front page when
changing shared styles. Open affected dialogs, filter a populated list, recover
from an empty result, tab through controls and check reduced motion and overflow.
Use available browser tooling; reload after asset edits. For application-wide
changes, cover representative chat, Settings, historical views, permission states
and panels rather than only the final edited screen.

Capture comparable rendered evidence when it helps review. Label synthetic data
and keep production account information out of repository screenshots. Report any
untested browser, assistive-technology, provider or production behavior accurately.
