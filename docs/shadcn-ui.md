# Moyai UI components

The workspace and every settings route render shared React components from
`frontend/components/ui`. These are shadcn/ui's New York components, backed by
Radix UI. The workspace palette and layout remain in place.

## Build and preview

```sh
npm ci
npm run build
npm run typecheck
npm test
npm run test:ui
npm run preview -- --port 8840
```

Open `http://127.0.0.1:8840`. This preview serves the real frontend with synthetic
API fixtures and cannot call models or modify connected services. `?fixture=empty`,
`?fixture=error` and `?fixture=member` select alternate states. Use `npm run dev` in
another terminal to rebuild on source changes, then refresh the browser.

The production bundle lives in `app/static/ui` and is checked in. Python, Docker
and Modal serve the same assets without a Node runtime. CI rebuilds the bundle and
checks that it matches its source. Commit bundle changes together with source.

The session integration suite uses real local session APIs and demo execution:

```sh
uv run python scripts/session_ui_demo.py --port 8830
SESSION_UI_URL=http://127.0.0.1:8830 node --test tests/browser/session_ui.cjs
```

Install the test browser once with `npx playwright install chromium`.

## Rendering contract

Feature controllers keep their existing API calls, revisions, access checks,
stream handling and escaped templates. `MoyaiUI.render(element, html)` converts
each template into a React tree composed of shadcn Button, Input, Textarea,
Select, Checkbox, Switch, Label, Badge, Table, Card, Alert, Collapsible and
Tooltip components. The initial workspace shell uses the same renderer.

This is a component migration, not a rewrite of the application state into React
hooks. The adapter is explicit at every rendering boundary. It does not patch DOM
prototypes or watch the DOM to replace controls after handlers have been bound.

- Use `MoyaiUI.render` for replacement, `insert` for incremental insertion, and
  `replace` for replacing a loading region. All commit synchronously so controllers
  can bind handlers immediately.
- Regions are replaced in full, matching the former `innerHTML` behavior. Do not
  call `root.render` to reconcile DOM owned by a controller. Nested roots are
  disposed before their parent, and a removal observer releases detached regions.
  A native `<template>` is only an inert parser, never a render host: mount streamed
  content on the element that joins the document so its root is released on removal.
- When replacing a transcript while reusing its activity slots, pass those nodes
  in `render(host, html, { preserve: slots.values() })` and reattach them
  synchronously. Their nested roots and component state remain live; preserved
  nodes that are not reattached are released by the removal observer. Do not
  serialize rendered components back into templates to keep their content.
- Inputs remain uncontrolled. Controllers can read and set `value`, run native
  validation, use `FormData`, and retain drafts while requests are in flight.
  The adapter explicitly strips controlled form props, independently of the HTML
  parser: editable inputs use `defaultValue`, choice controls use `defaultChecked`,
  and a checkbox/radio `value` remains its submitted identity. Action inputs keep
  their value labels; file inputs never receive a prefilled value. Textareas seed
  their text content, and selects seed the selected options. Keep this policy in
  the shared renderer, not individual page templates.
  The Checkbox/Switch adapter preserves the existing `checked` and native
  `input`/`change` event contract, including reset and failed-save rollback.
- `MoyaiUI.createDialog()` returns a persistent content host with `showModal`,
  `close`, `open`, `returnValue`, `cancel` and `close` events. Its visible surface is
  shadcn Dialog; Radix provides the portal, focus trap, Escape, outside dismissal
  and return focus. The host remains queryable while closed, and sensitive forms
  keep their existing close/cancel cleanup.
- Single-choice dropdowns use shadcn Select with an anchored, viewport-aware
  menu, not the operating system popup. `FormSelect` retains an invisible native
  select for controller queries, sizing, validation, reset, and `FormData`.
  Only the styled trigger is exposed to keyboard and assistive technology.
  Per-element value/selectedIndex setters and an option/attribute observer keep
  controller updates in sync; they never replace controller-owned DOM or patch
  global prototypes. Selection emits one native input/change pair. Synchronizing
  values must not emit writes. Multi-select/listbox templates retain NativeSelect.
- Native option trees are controller-owned from their first mount. Rendering into
  a select replaces parsed native options without mounting a nested React root.
  Keep selected attributes as reset defaults, and retain labels, disabled groups
  and submitted values. Label activation is cancelled on the hidden native select
  and opens the styled menu instead; only the styled trigger receives focus.
- Settings polling must check `settingsInteractionActive()` before a request and
  again before committing its result. An open Select is active even while its
  focused menu is portaled outside the page content.
- Nested Select menus consume Escape before the enclosing dialog or workspace.
  Keep this guard in the overlay adapter: feature forms can have independent React
  roots even when they appear inside a Dialog portal.
- Session actions use shadcn Popover anchored to the invoking control, with Radix
  viewport collision handling. The existing menu commands and arrow-key handlers
  remain in their controller.
- Rich-text skill atoms, Markdown sanitization, charts, native drag/drop and the
  remote computer canvas retain their specialized implementations. Their surrounding
  controls use the shared components. Plain structural HTML remains semantic HTML.
- Templates are trusted application markup; continue to escape API values and
  sanitize Markdown before rendering. The component adapter is not a sanitizer.

## Styling

`components.json` configures the official shadcn CLI and TypeScript aliases.
`frontend/theme.css` maps its semantic roles to Moyai's existing purple, lavender,
white and neutral colors. Tailwind utilities are scoped through CSS layers and do
not introduce Preflight, so existing page geometry stays intact. Component rules
for portals, fields and controls are shared here; do not add page-specific copies.

The legacy styles still own page and chat layout. Native dialog selectors now
target `[data-slot="dialog-content"]`; per-dialog sizing uses `data-dialog-id`.
The content host uses `display: contents`, so measure or focus the visible dialog
via its dialog role or `data-dialog-id`, not the inner host.

The template adapter removes Card's default flex layout and gap. Existing page
styles already space the card contents; applying both doubles the gaps in
Connections, Memory, and Environments. New React-only cards can use the normal
shadcn CardHeader/CardContent structure.

## Verification

Controller tests use a renderer double in `tests/helpers/ui-vm.cjs`; their API,
escaping, race and permission assertions remain independent of React. Browser
tests exercise the built components, actual form values, dialog focus/cancellation,
saved preferences, filters, role restrictions, errors and 1440/768/320px layouts.
Menu placement is verified in Chromium rather than by mocking Radix geometry.
The dropdown suite opens every rendered single-choice menu across the primary
pages and editors at 1440/768/320px, checks field/menu separation, wrapping,
keyboard focus, Escape, and the native form bridge.
