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

`?fixture=skill-picker` provides personal and organization skills with duplicate
and long names. Type `/team` in the new-session composer, or open
`?fixture=skill-picker#run=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` to check the reply
composer. Both use the production picker against synthetic data.

`?fixture=agent-sidebar` provides main agents, subagents and a grandchild with
unread, running, approval and failure indicators. Expand the first session and
its Explore agent to inspect disclosure, title and status alignment.

`?fixture=account-links#spend` provides synthetic Slack/Google accounts and pending
costs. Open Infrastructure, expand Administrator overrides, choose an account,
then click the top bar to leave the form idle. The five-second cost poll keeps the
account-link controls mounted, retains the selection and disclosure, and fetches
updated status independently. Transient lookup errors retain usable controls;
authorization failures remove them. The navigation browser suite also covers
changed account data, retries, an open Select and leaving the route mid-refresh.

Completed account-link updates have their own deferred paint, independent of
pending costs. While a menu is open or the tab is hidden, one local timer checks
every 250 ms and retains the response without making more requests. Once the view is idle,
it captures the current drafts and applies that response. New refreshes and
account actions supersede older updates; route, account, role, scope, date and
mounted-panel checks fence both in-flight responses and deferred paints. Access
failures clear the controls immediately. The regression for the last cost poll
fails against `0f579cf` and passes with this lifecycle.

`?fixture=account-links-final#spend` settles costs on the first background poll
and changes the identity status two seconds later. Open Infrastructure and
Administrator overrides, then click the top bar. After the five-second refresh,
open the account menu during that lookup delay. It stays open when the response
arrives; choose an account and click the top bar to see the deferred status apply
without another cost poll or network request. Reload to restart the fixture.

The production bundle lives in `app/static/ui` and is checked in. Python, Docker
and Modal serve the same assets without a Node runtime. CI rebuilds the bundle and
checks that it matches its source. Commit bundle changes together with source.

The session integration suite uses real local session APIs and demo execution:

```sh
uv run python scripts/session_ui_demo.py --port 8830
SESSION_UI_URL=http://127.0.0.1:8830 node --test tests/browser/session_ui.cjs
```

Install the test browser once with `npx playwright install chromium`.

Navigation reads reuse a bounded, in-memory cache in `navigation-cache.js`:
40 responses, an 8 MiB serialized-data budget, and a 15-second reuse window.
Conversation snapshots may paint for up to 60 seconds while a fresh request and
the event stream update them without remounting the composer. Responses are
copied before controllers mutate them. Nothing is persisted in browser storage.
Writes invalidate at both request boundaries; account/role changes and access
errors clear the cache. Explicit Refresh and background polls still fetch fresh
data. Secrets and credential forms are excluded from the allowlist.

The sidebar requests the compact `view=sidebar` session list, with batched root
reads and lightweight agent rows. See [session list performance](session-list-performance.md)
for the real API demo, before/after measurements and database-wait regression.

Hover intent (80 ms) and keyboard focus preload the chosen page, analytics tab,
conversation, or a PR linked to the current session. Speculative reads stop when
three cacheable requests are in flight and are disabled in hidden tabs or with
the browser's Save-Data preference. PR diffs share the same cache; Refresh always
revalidates. Spend charts do not wait for Infrastructure's account-link settings.

For a populated navigation demo with controlled transport latency:

```sh
node scripts/settings_ui_preview.cjs --port 8840 --delay-ms 300
node --test tests/browser/navigation_loading.cjs
```

The browser test starts its own fixture server. The preview URL is localhost only
and uses synthetic API responses. `--root /path/to/baseline/app/static` serves
the same data and latency with another frontend. In one 1440px Chromium comparison
against `cb077b4`, click-to-next-frame timings were: Users return 608 → 14 ms,
Leaderboard return 916 → 16 ms, first Spend 618 → 317 ms, and a preloaded PR report
319 → 37 ms. These are local samples, not production percentiles or a cold-load
guarantee; expired/oversized entries and missing preloads still require a request.

With the chat and PR demo servers below running, also run:

```sh
CHAT_LOADING_URL=http://127.0.0.1:8877 PR_LOADING_URL=http://localhost:8880 \
  node --test tests/browser/navigation_sessions.cjs
```

This verifies a usable cached conversation while revalidation is held, draft and
composer retention, shared hover/click PR reads, reopening a diff, and explicit
Refresh. With real local APIs and synthetic GitHub reads, a 40-file PR reopened
in 25 ms versus 576 ms; a conversation returned in 15 ms versus 298 ms. These
samples used the demo's 250 ms delays, not production GitHub or SSO.

To reproduce loading and sidebar behavior with real local session APIs:

```sh
uv run python scripts/chat_loading_demo.py --port 8877
```

Open the printed login URL. This seeds SQLite with 100 synthetic sessions,
60 child agents and 500 tool events, and adds controlled request delays.
The diagnostic panel reports conversation/list readiness, group toggle timing,
retained DOM nodes and history requests. Expand a completed turn to fetch its
history, or send a message to exercise the local demo executor. It does not
exercise production SSO, models or connected services. Use `--delay-scale 0`
for local processing timings, or `--root /path/to/baseline --port 8878` to run
the same data and delays against another checkout.

To compare PR loading and panel section switching:

```sh
uv run python scripts/panel_loading_demo.py --port 8880
```

Open the printed localhost login URL and the first PR. This reuses the real
session APIs and temporary SQLite with 40 synthetic changed files, 80 diff lines
per file, and 250 ms per simulated GitHub read. No external GitHub request is
made. The diagnostics measure section switching, mounted diff rows and retained
content. `/demo/panel-measurements` records the actual upstream request sequence.
Use `--source-root /path/to/baseline --port 8881` for an identical comparison,
or `--github-delay-ms 0` to remove the injected delay.

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
- Bind controller click handlers on tooltip triggers with `addEventListener`,
  not the DOM `onclick` property. React updates that property when the tooltip
  opens or closes; native listeners survive those component updates.
- Regions are replaced in full, matching the former `innerHTML` behavior. Do not
  call `root.render` to reconcile DOM owned by a controller. Nested roots are
  disposed before their parent, and a removal observer releases detached regions.
  A native `<template>` is only an inert parser, never a render host: mount streamed
  content on the element that joins the document so its root is released on removal.
- Repeated sidebar and transcript items use `MoyaiRegions.sync(host, html)`.
  A `data-region-key` identifies a native, controller-owned container within its
  parent. A `data-region-leaf` renders its content through `MoyaiUI` only when
  that item's template changes, retaining other items and their component state.
  A `data-region-preserve` container leaves independently updated activity or
  credential content in place. Omit collapsed children until they are expanded.
  Keep keys stable and unique within each parent; never reconcile controller
  mutations through a parent React root or serialize rendered components back
  into templates.
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
  Dialogs use their own portal mount. Set `host.dataset.dialogScope = 'settings'`
  for Settings editors; workspace dialogs are the default. Reused hosts must set
  their scope for each flow before opening. Route styles stay on the sidebar and
  workspace containers, so global dialogs never inherit Settings form styles.
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
`frontend/theme.css` maps its semantic roles to Moyai's deep-space navy, icy blue,
ivory and semantic status colors. Tailwind utilities are scoped through CSS layers and do
not introduce Preflight, so existing page geometry stays intact. Component rules
for portals, fields and controls are shared here; do not add page-specific copies.

The legacy styles still own page and chat layout. Native dialog selectors now
target `[data-slot="dialog-content"]`; per-dialog sizing uses `data-dialog-id`.
The content host uses `display: contents`, so measure or focus the visible dialog
via its dialog role or `data-dialog-id`, not the inner host.

Template buttons default to automatic height, wrapping text and flexible shrink,
so attachment thumbnails, multiline choices and narrow action rows stay inside
their controls. Explicit template classes and feature styles still own sizing,
including fixed-size icon buttons. Direct React `Button` defaults are unchanged.

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

## Composer message history

In a conversation, Up at the start of the reply text recalls the signed-in
user’s previous messages. Repeated Up/Down browses that history; Down past the
newest entry restores the unsent draft. Moving the caret or editing returns
to ordinary text navigation until the caret reaches the start/end again.
History is local to the mounted conversation, excludes other participants,
and recalls text only, without reattaching files. Slash-picker navigation
takes priority. The new-session composer has no conversation history.

`node --test tests/browser/message_history.cjs` checks the real composer
against synthetic responses at desktop, tablet and mobile widths.
