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

The home sculpture uses the supplied Meshy **Silent Stone Orbit** model, converted
offline from FBX into `native/web/public/moyai.glb`. Its base color, normal and
packed metallic/roughness textures use the model's UVs and standard PBR material.
The optimized asset has 50,150 triangles and 2048px embedded textures; importing
and sculpting it is not a runtime or normal build step. See
[the asset provenance](../scripts/assets/moyai-model.md) for conversion details.
The offline art pass widens the meteor's depth, gives its oval outline a slight
lean, and varies ridge sizes while protecting the face. Preserve its framing
against the viewer's camera when editing the geometry.
`frontend/moyai-surface.ts` adds object-space grain, pitting and mineral glints
to the supplied PBR material. The baked `_MOYAI_CAVITY` attribute localizes blue
and violet highlights to authored crater bowls, with faint concavity accents.
Glints respond to rotation without a continuous animation loop. Avoid a
photo-projection shader or a separate overlapping sheen mesh.

The Three.js viewer remains lazy and supports drag and keyboard rotation.
It disposes shared geometry, materials, textures, the reflection target and shadow
maps once on unmount. Embedded images use TextureLoader so the existing content
security policy can load them through `img-src`. The lightweight loading/WebGL
fallback is a transparent render of this same model at its initial camera angle.

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
