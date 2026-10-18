# Moyai UI components

The React application lives in `native/web`. It imports shared shadcn/Radix
controls from `frontend/components/ui` and semantic tokens from
`frontend/theme.css`: The space workspace uses near-black smoked glass surfaces, lavender actions,
local Manrope interface typography and semantic status colors. Shared glass tokens
provide surface tint, subtle highlights and blur for the sidebar, composer and
floating menus, with opaque fallbacks for unsupported blur or reduced transparency.
Desktop panels sit inside a rounded inset frame; mobile keeps the full viewport.
The wordmark,
home headline and code retain Share Tech Mono. Both fonts are served locally.
The retired escaped-template renderer is no longer used.

Use direct React components for buttons, dialogs, forms, selects and menus.
Preserve labels, focus behavior, portal geometry and permission-driven actions.
`native/web/settings.tsx` and `settings.css` own the Settings shell; product
adapters supply capability-specific forms. `style.css` and the focused panel,
sidebar and workspace styles own application layout.
Desktop sidebars collapse to a 52px rail with an open button at the top;
workspace and Settings share the collapsed state. Mobile navigation stays an overlay.

The orbit workspace key uses the shared Button and Radix Dialog behavior, including
Escape, outside dismissal and the Command/Control-period shortcut. Its resource
menu floats within the chat area and must not resize the transcript, move the
composer or change their scroll positions. The independent Computer/resource
side panel still resizes the chat through its existing split.

Images/fonts belong to `native/web/assets`; public files and the sculpture model
belong to `native/web/public`. Analytics rendering and automation normalization
are ordinary modules with type declarations. There are no build-time source
extractors or imports from a legacy static tree.

The home screen keeps the original meteor's frontal photograph and irregular oval
outline, with the supplied Meshy geometry for the sides and back.
`frontend/moyai-original-surface.ts` fades the reference image into the textured
stone as the rock turns. The Three.js viewer loads lazily and supports drag and
keyboard rotation; Home resets the view. The original image remains the loading
and WebGL fallback. See the [original-profile asset notes](../scripts/assets/original-profile.md).

The headline decodes once per home-screen visit; its cursor blinks five times,
then disappears. The rock stays still until rotated. Rendering stops offscreen
and in background tabs; reduced motion disables rotation inertia. Geometry,
materials, textures, reflection targets and shadow maps are disposed on unmount.

```sh
npm ci
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run test:ui
```

Generated `native/src/moyai/static` assets are ignored by Git. Docker and package
builds create them from source; wheels and source distributions carry the build
without requiring Node.js at runtime. `npm run dev` rebuilds on changes; use `uv run moyai dev` to serve the
application with its configured backend.

Browser tests in `tests/browser/native_*.cjs` serve the real React application
against synthetic APIs. They cover keyboard navigation, access revocation,
historical routes, attachments, responsive panels and settings. Inspect affected
screens at 1440, 768 and 320 pixels, including dialogs, focus, overflow and reduced
motion. Fixture screenshots demonstrate presentation, not live provider execution.
See the [repository UI skill](../.agents/skills/moyai-ui/SKILL.md) before changes.
