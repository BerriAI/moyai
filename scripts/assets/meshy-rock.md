# Silent Stone Orbit asset

The home-screen rock comes from
`Meshy_AI_Silent_Stone_Orbit_1017192604_texture_fbx.zip`, supplied by the user.
It uses the FBX geometry and its color, normal, roughness and metallic maps.
There is no photograph overlay or material change based on viewing angle.

The conversion reduces 1,433,008 triangles to 49,998 and scales the texture maps
to 2048 pixels. The GLB is about 3.4 MB. Its matching transparent render is used
for the loading fallback, sign-in screen and empty chats.

To rebuild with Blender 4.5, extract the archive and run from the repository root:

```sh
blender --background --factory-startup --python scripts/assets/import_meshy_rock.py -- \
  path/to/extracted-textures .data/meshy-rock.glb
cp .data/meshy-rock.glb native/web/public/moyai.glb
uv run python -c 'from PIL import Image; Image.open(".data/meshy-rock.png").save("native/web/assets/moyai-meteor-poster.webp", quality=92)'
npm run build
```

The script centers and scales the source, preserves its UVs, and embeds the
textures. It also renders a PNG at the viewer's starting three-quarter angle
(yaw 0.6, pitch -0.1 radians) for the fallback. Keep the render pose in sync with
`initialYaw` and `initialPitch` in `frontend/moyai-sculpture.ts`.
Verify the front, side and back in the app after rebuilding.
