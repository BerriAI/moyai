# Original front, full 3D profile

This alternative keeps `moyai-meteor-reference.webp` as the front appearance and
fits the supplied Silent Stone Orbit geometry to its off-center outline. Its
sides, craters and face profile come from the full supplied mesh rather than the
previous inferred height field.

The front image fades as the object turns. It is fully visible at Home rotation;
the normal PBR material is fully visible beyond about 40 degrees. The transition
is view-dependent and deterministic, with no timer or continuous animation.
This hybrid preserves the preferred frontal composition; it is not a claim that
the original image was reconstructed as an exact 3D scan.

## Rebuild

Use the original optimized Meshy input from commit
`68c689b93bcf262590e7067a3f42332d1245b82b`, before the separate sculpting pass.
The script verifies its SHA-256:
`af74b964b07d954a156893f441850e7e72186d11baa0c854e9f17d21e2536a05`.
The unmodified texture PNGs come from the supplied
`Meshy_AI_Silent_Stone_Orbit_1017192604_texture_fbx.zip`.

```sh
npm ci
npm install --prefix work/meteor-tools \
  @gltf-transform/core@4.5.1 @gltf-transform/functions@4.5.1 sharp@0.35.5
MOYAI_ASSET_TOOLS="$PWD/work/meteor-tools" node scripts/assets/build_original_profile.mjs \
  path/to/original-meshy.glb scripts/assets/moyai-meteor-reference.webp \
  path/to/extracted-textures native/web/public/moyai.glb
```

The pass bakes the starting orientation into the geometry, derives a 720-sample
outline from the reference alpha, and fits projected vertex positions to it.
Interior changes use a smoothed envelope to protect the face from fine outline
variations. Normals are recalculated. Original UVs keep the supplied stone maps;
`TEXCOORD_1` carries the camera-matched photograph.

The reference is embedded in the standard emissive texture slot with a black
emissive factor. The viewer transfers that texture to the reference layer and
uses its alpha to preserve the frontal outline. Both layers share geometry and
are released together. No new loader or runtime package is required.

Output: 35,035 vertices, 50,150 triangles, 2048px body color/normal maps, and the
original reference image. GLB size: 6,306,868 bytes. SHA-256:
`ed2fe442b87b4c2623e48a23d9d15b19e3b5e05a23a3240f923a0feb2693cb66`.
The extra reference texture is a deliberate download-size tradeoff.

The old `generate_moyai_meteor.py` remains available to reproduce the previous
height-field asset; it does not produce this variant.
