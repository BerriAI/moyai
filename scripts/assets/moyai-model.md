# Home sculpture: Silent Stone Orbit

Source supplied for the home screen on October 17, 2026:
`Meshy_AI_Silent_Stone_Orbit_1017192604_texture_fbx.zip`.

Source archive SHA-256:
`00b6a49d42a85fdfd4bfbaed513221a42917e55ca5706f75b45eb1b218ed054a`.
The original 83 MB archive is kept outside the repository.

## Original offline conversion

1. Load the FBX with Three.js 0.180.0 `FBXLoader`. It contains one mesh with
   1,433,008 triangles. Preserve its imported orientation and UVs.
2. Replace the imported Phong material with a white `MeshStandardMaterial`.
   Preserve the supplied base-color and normal maps; load the supplied roughness
   and metallic PNGs into their corresponding slots with both scalar factors set
   to `1`. Do not rely on the FBX Phong shininess/reflection slots to carry PBR maps.
3. Subtract the world bounding-box center from the model position. Place it in a
   group scaled uniformly by `3.5 / longestBoundingBoxSide`.
4. Export with Three.js `GLTFExporter`, `{binary: true, maxTextureSize: 1024}`.
   The exporter packs roughness into green and metallic into blue.
5. With glTF Transform 4.5.1, meshoptimizer 1.3.0 and sharp 0.35.5, apply
   `weld()`, `simplify({simplifier: MeshoptSimplifier, ratio: 0.035, error: 0.001,
   lockBorder: false})`, `dedup()`, `prune()` and
   `textureCompress({encoder: sharp, targetFormat: "jpeg", quality: 90,
   resize: [1024, 1024]})`.
6. Save the result as `native/web/public/moyai.glb`. It contains 35,035 vertices,
   50,150 triangles and three embedded textures in a standard PBR material.
   It is 2,223,368 bytes and requires no additional loader/decoder extensions.
7. Render the initial view with the actual home-screen renderer at device pixel
   ratio 2. Export its transparent 440×440 canvas after rendering and encode WebP
   at quality 94 as `native/web/assets/moyai-meteor-poster.webp`.

Original optimized GLB SHA-256 (before the art pass):
`af74b964b07d954a156893f441850e7e72186d11baa0c854e9f17d21e2536a05`.

## Sculpted and weathered version

The current asset applies `sculpt_moyai_meteor.mjs` to that original optimized GLB.
The script checks the source hash so the deformation cannot accidentally be
applied twice. It flattens the imported transforms, widens a yawed ellipsoid around
the protected face, leans the surrounding meteor and varies ridge sectors. No
triangles are added: it still has 35,035 vertices and 50,150 triangles.

The original 2048px base-color, normal, roughness and metallic PNGs are restored
from the supplied archive. Their image rows must be flipped to match the FBX to
glTF exporter convention. Surface normals are recalculated after deformation,
with shared-position adjacency across UV seams.

The `_MOYAI_CAVITY` scalar attribute combines hand-placed crater bowl volumes with
a weak smoothed concavity signal. Bowl axes and depth caps keep highlights off
the surrounding boulders. The runtime material adds fine object-space grain and
pitting, high stone roughness, restrained blue/violet interior highlights, and
sparse reflective mineral flecks. The material has no clock or extra animation
loop; the same angle always renders the same detail.

Reproduce using Node.js and temporary offline dependencies (not runtime packages):

```sh
npm install --prefix work/meteor-tools \
  @gltf-transform/core@4.5.1 @gltf-transform/functions@4.5.1 sharp@0.35.5
git show 68c689b93bcf262590e7067a3f42332d1245b82b:native/web/public/moyai.glb \
  > work/silent-stone-original.glb
MOYAI_ASSET_TOOLS="$PWD/work/meteor-tools" node scripts/assets/sculpt_moyai_meteor.mjs \
  work/silent-stone-original.glb path/to/extracted-textures native/web/public/moyai.glb
```

Current GLB: 5,134,880 bytes, SHA-256
`d75aaa45b1b7ba3f6d37af11d9f2c4da94864d61dc97648760ae2345100d4d7e`.
The larger textures are an intentional quality tradeoff for close inspection;
the model remains lazy-loaded and uses no new decoder.

Regenerate the poster from the final viewer at Home rotation with a square,
transparent canvas. Downsample to 440×440 and encode WebP at quality 94. An offline
capture may preserve the drawing buffer to export the image; leave that option
disabled in the interactive application.

The earlier `generate_moyai_meteor.py` reproduces the previous photograph-based
asset. It is not the generator for this supplied model.

## Verification

The model uses the existing same-origin GLB route and `TextureLoader` image path.
Check the real application's content security policy, front/side/back rendering,
drag and keyboard rotation, Home reset, route remount, reduced motion, and the
loading/WebGL fallback. Inspect 1440px, 768px and 320px layouts.
