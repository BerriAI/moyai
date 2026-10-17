# Moyai UI components

The React application lives in `native/web`. It imports shared shadcn/Radix
controls from `frontend/components/ui` and semantic tokens from
`frontend/theme.css`: The space workspace uses near-black neutral surfaces, lavender actions,
local Share Tech Mono typography and semantic status colors. The retired escaped-template renderer is no longer used.

Use direct React components for buttons, dialogs, forms, selects and menus.
Preserve labels, focus behavior, portal geometry and permission-driven actions.
`native/web/settings.tsx` and `settings.css` own the Settings shell; product
adapters supply capability-specific forms. `style.css` and the focused panel,
sidebar and workspace styles own application layout.

The orbit workspace key uses the shared Button and Radix Dialog behavior, including
Escape, outside dismissal and the Command/Control-period shortcut. Its resource
menu floats within the chat area and must not resize the transcript, move the
composer or change their scroll positions. The independent Computer/resource
side panel still resizes the chat through its existing split.

Images/fonts belong to `native/web/assets`; public files and the sculpture model
belong to `native/web/public`. Analytics rendering and automation normalization
are ordinary modules with type declarations. There are no build-time source
extractors or imports from a legacy static tree.

The meteor is an actual cratered mesh, generated offline with
`uv run --script scripts/assets/generate_moyai_meteor.py`; generation is not a
runtime or normal build step. Its pinned script dependencies produce the bundled
GLB with matte material, baked vertex shading and embedded mineral surface maps.
Use cool gray lighting and a restrained rim light; avoid a CSS glow around the silhouette.
The Three.js viewer remains lazy,
supports drag and keyboard rotation, and disposes its geometry, materials and
textures/shadow maps on unmount. Embedded images use TextureLoader so the existing
content security policy can load them through `img-src`. The supplied reference
image is the lightweight fallback.

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
