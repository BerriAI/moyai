---
name: moyai-ui
description: Build or refine Moyai frontend pages using the repository’s established navigation, typography, spacing, forms, tables, and interaction patterns. Use for changes to app/static and UI reviews; not for backend-only work.
---

# Moyai UI

Keep Moyai’s calm, practical workspace identity: white content, a quiet lavender rail, neutral typography, purple for the primary action and selection. A new screen should look like a sibling of the existing screens. The current implementation and [screenshot gallery](../../../docs/settings-redesign.md) are the visual reference; do not start a new aesthetic for a feature.

## Read the source of the primitive

- `frontend/components/ui`: the shared shadcn/ui components. `frontend/render.tsx` maps the existing escaped feature templates to these components; `frontend/overlays.tsx` supplies shadcn Dialog and Popover. Read [the rendering contract](../../../docs/shadcn-ui.md). Build with `npm run build` after editing component sources and commit the generated `app/static/ui` assets with the source.
- `frontend/theme.css`: shadcn semantic color roles, shared component sizing, and portal geometry. Existing page and chat styles still own layout.

- `app/static/settings-system.css`: the shared Settings layout, tokens, headers, controls, lists, notices, tables, dialogs, and responsive rules. Make shared changes here instead of layering a new override file over it.
- `app/static/settings.js`: navigation registry (`settingsGroups`), overview, shared filter behavior, retry state, and `confirmSettingsAction()` for cancellable destructive actions.
- `app/static/branding.css`: workspace color identity. `polish.css` and `style.css` supply the existing chat and sidebar layout. Settings styles are scoped by `.settings-view` so they do not change the composer.
- `app/static/icons.js`: shared outline icons. Settings destination icons live in `settingsIcon()`. Reuse these before drawing new glyphs.
- `app/static/index.html`: script/style order and dialog content hosts. Feature scripts call `MoyaiUI.render` with escaped templates and bind handlers after the synchronous React commit. Keep API/state logic in the controllers and component behavior in the shared frontend layer.

Use the existing React/shadcn stack and do not restyle chat for a Settings-only task. Inspect both the front page and the changed page when touching shared styles.

## The primitives

| Primitive | Convention |
| --- | --- |
| Page shell | `.settings-view`, one persistent Settings rail, breadcrumb in the topbar, content measure of 1056px, 40px top inset and 32px minimum desktop side inset. Mobile content uses 16px side insets. |
| Navigation | Add destinations to `settingsGroups` once. Overview and rail use the same registry and admin visibility. Links use hashes and `aria-current`; “Back to workspace” returns to chat. |
| Page heading | One `h1`, 28px/1.25, weight 600; short purpose sentence at 14px. Align the primary action with the heading. No all-caps eyebrow repeating the breadcrumb. |
| Text | Keep the workspace’s system font stack. Body/control text 14px, captions 13px, section headings 16px/1.4. Use the role tokens, unitless line heights, and weights 400–600. Keep prose around 65–75ch. |
| Spacing | Use 4, 8, 12, 16, 20, 24, 32, 40px steps. Related labels/controls are close; separate sections by at least twice that space. Preserve shared leading edges. |
| Surfaces | Flat white surfaces; 12px cards, 8px controls. Borders organize tables and groups. Shadows are reserved for dialogs and overlays. Do not nest a card inside every card. |
| Lists | Repeated library objects use compact rows with identity first, supporting copy next, actions last. Search/filter before a large library. Preserve full labels and identifiers or provide an explicit expansion. |
| Controls | One filled purple primary action; secondary buttons neutral, quiet actions in consistent action areas. Use the shared shadcn Button, Label, Input, Select, Checkbox, Switch and Collapsible through the component renderer. Single-choice menus anchor outside their field; do not reintroduce platform popups. 36–40px desktop controls; touch controls at least 44px when space permits. |
| State | Pair status text with a semantic treatment. Green = ready, amber = incomplete/needs attention, red = destructive/error. Purple is selection and primary action, not a generic status signal. |
| Details | Use `<details><summary>` for longer explanations, automation instructions, billing methodology, and history. The summary names the hidden content. Never clamp essential text without a way to read all of it. |
| Tables | Quiet header surface, clear row rules, tabular numbers, numeric totals at the trailing edge. Keep exact costs in request detail; round summary amounts to cents. At narrow widths, scroll the table’s own labelled, keyboard-focusable container. |
| Dialogs | Use `MoyaiUI.createDialog()` and the existing `showModal()`/`close()` controller contract. The visible surface is shadcn Dialog with focus trapping and Escape. Give the dialog a name, explicit labels, an error region, and a visible close/cancel control. Measure `[data-slot="dialog-content"]`, not its inner content host. Cap height to the viewport and contain scrolling. |
| Empty/error | Explain the state and offer the next action. Filtered emptiness has a clear-filters action. Failed loading has a retry; keep API details escaped. |

Color values come from the existing workspace palette and `--settings-*` role tokens. Avoid adding almost-identical hex values in feature files. Measure contrast when changing text, borders, or focus colors; keep ordinary text at 4.5:1 and meaningful control boundaries/focus at 3:1. Do not introduce dark mode as an incidental change.

## Interaction invariants

- Preserve API contracts, permission checks, saved scopes, revisions, and all existing actions. Personal/organization scope is a real access boundary, not decoration.
- Every async renderer must check `state.pageVersion` before replacing content. Loading must not leave another page’s controls under a new title.
- Route changes update the title and focus the new heading. Refreshing a page in place must preserve active filters, expanded details, and useful focus rather than reset the user’s task.
- Provide visible keyboard focus. Do not remove outlines from interactive elements. A programmatically focused route heading may omit the ring.
- A destructive action explains what will happen and supports cancellation or undo. Keep the least destructive choice focused in a custom confirmation.
- Mobile inputs use 16px text to avoid iOS zoom. Use `minmax(0,1fr)`, wrapping action groups, logical inline properties, and word wrapping for long identifiers.
- Keep state changes instant unless motion conveys useful information. Guard movement with `prefers-reduced-motion`; no entrance animation on every refresh and no `transition: all`.

## Verify the actual screen

Run relevant Node regressions (`node --test tests/*.cjs`) and any backend tests warranted by changed behavior. A visual-only adjustment does not need a new test that merely checks its CSS string.

For reproducible UI inspection:

```sh
node scripts/settings_ui_preview.cjs --port 8840
```

The preview serves the real frontend against synthetic API fixtures. `/?fixture=empty`, `/?fixture=error`, and `/?fixture=member` exercise alternate states. It binds only to localhost and does not exercise production authentication, cloud builds, or provider writes. For a matched baseline, use `--root /path/to/baseline/app/static --port 8841`.

Inspect the changed route at 1440px, 768px, and 320px; open affected dialogs, filter a populated list, clear an empty result, tab through controls, and check reduced motion. Browser reload is required after editing static files. Use the browser tooling available in the current environment; do not assume one automation driver is installed.

For migration-wide work, apply all six `better-interface` domains across the
workspace and every Settings route, not just the last reported page. Include
representative editors, nested menus, empty/error/member fixtures, enlarged text,
and keyboard-only paths. Verify shared geometry in the rendered app: component
defaults must not add a second layer of spacing to legacy layouts. Measure
contrast against the actual surface and reuse existing role tokens for fixes.
Report untested screen-reader, browser, provider, and production flows explicitly.

Capture real rendered before/after images with the same viewport and data. Label synthetic previews honestly. Keep production account data out of public repository screenshots. Document checks that could not be run instead of claiming accessibility or interaction coverage from a screenshot alone.

These conventions apply Nielsen’s visibility, consistency, recognition, user control, error recovery, and minimalist-design principles. The supporting design review was informed by [better-interface and its six domain skills](https://github.com/jakubkrehel/skills/tree/main/skills/better-interface). Future work should follow the concrete Moyai primitives above, rather than copy a generic redesign recipe.
