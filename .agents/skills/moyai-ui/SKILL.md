---
name: moyai-ui
description: Build or refine Moyai's native React frontend using its established navigation, typography, forms and interaction patterns. Use for native/web and shared frontend changes or UI reviews, not backend-only work.
---

# Moyai UI

Preserve the compact workspace: neutral charcoal and white themes, fine borders,
restrained purple actions and frosted glass navigation, toolbars and composers.
Keep code and reading surfaces quiet. Use solid fallback surfaces when blur is
unavailable or reduced transparency is requested; avoid blur on layout ancestors. Theme choice persists locally;
dark is the default. Keep messages and composer focus borders neutral. Use General Sans
from the official Fontshare stylesheet for the interface, local Geist as the
fallback, and Geist Mono for code. Do not commit the restricted General Sans
font binaries to this public repository.
Home and chat share a sparse static dot grid with faint guide lines and translucent glass composers.
Home includes compact draft starters; code and message bubbles retain solid reading surfaces. Use 6–8px rounded controls within
an edge-to-edge workspace without an outer gutter or rounded frame. The default sidebar groups current work by Needs you,
In progress, In review and Backlog; preserve the alternate folder view and
its existing actions. Keep chat entry compact, with attachment/model tools below. Use monospace for code,
paths and technical metadata, with General Sans for prose. Keep review diffs prominent
and expose repository/commit metadata through the Review details disclosure.
The workspace menu floats without reflowing chat; only the independent side
panel changes chat proportions. Inspect current screens before changing them.

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
not alone. Green means ready/open, yellow needs input, red means error/closed,
and purple means merged. Keep sidebar PR statuses as text and icons, without PR numbers or colored row fills.
Dialogs need names, labels, errors, cancellation and contained scrolling.
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

## React performance

Apply the relevant client-side guidance from
[Vercel React Best Practices](https://github.com/vercel-labs/agent-skills/tree/main/skills/react-best-practices).
Keep heavy features such as the Three.js sculpture dynamically imported, isolate
frequent animation state from form and application state, hoist deterministic
decorations, and clean up timers, observers and event listeners. Pause decorative
work offscreen, in background tabs and for reduced motion. Do not add a dependency
or a Next.js-specific pattern to this Vite app solely to satisfy an example.

## Verify

Run `npm run typecheck`, relevant `npm test` cases, `npm run build` and
`npm run test:ui`. The browser fixtures serve the actual React app against synthetic
APIs; they do not prove provider authorization or production execution.
Finish the build before API tests that mount generated static files. Avoid source
edits during browser suites, since hot reload can invalidate in-flight checks.

Inspect affected screens at 1440px, 768px and 320px. Include the front page when
changing shared styles. Open affected dialogs, filter a populated list, recover
from an empty result, tab through controls and check reduced motion and overflow.
Use available browser tooling; reload after asset edits. For application-wide
changes, cover representative chat, Settings, historical views, permission states
and panels rather than only the final edited screen.

Capture comparable rendered evidence when it helps review. Label synthetic data
and keep production account information out of repository screenshots. Report any
untested browser, assistive-technology, provider or production behavior accurately.
