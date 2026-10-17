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

This variant keeps the original meteor's frontal photograph and irregular oval
outline, while using the supplied full Meshy geometry for the sides and back.
`scripts/assets/build_original_profile.mjs` fits that geometry to the reference
outline and embeds a second UV set for the front image. Generation runs offline.
See [the original-profile asset notes](../scripts/assets/original-profile.md).

`frontend/moyai-original-surface.ts` owns the view-dependent reference layer.
At Home rotation it displays the original photograph; as the rock turns, that
layer fades to the underlying textured 3D stone. By about 40 degrees, only the
physical surface remains, avoiding stretched front-photo details in profile.
This is an intentional hybrid presentation, not a recovered scan of the image.
Keep the projection's 8.6-unit camera distance aligned with the viewer.

The Three.js viewer remains lazy and supports drag and keyboard rotation.
It disposes shared geometry, materials, textures (including an unmounted model's
embedded reference image), the reflection target and shadow
maps once on unmount. Embedded images use TextureLoader so the existing content
security policy can load them through `img-src`. The original reference image
remains the lightweight loading/WebGL fallback.

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
