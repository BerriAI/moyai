import * as THREE from 'three'

/** Object-space detail stays attached to the stone as it turns, including across UV seams. */
export function finishMeteorSurface(mesh: THREE.Mesh) {
  if (!mesh.geometry.hasAttribute('_moyai_cavity')) return
  const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
  for (const material of materials) {
    if (!(material instanceof THREE.MeshStandardMaterial)) continue
    material.normalScale.set(1.4, 1.4)
    material.onBeforeCompile = shader => {
      shader.vertexShader = shader.vertexShader.replace('#include <common>', `
        #include <common>
        attribute float _moyai_cavity;
        varying vec3 moyaiSurfacePosition;
        varying float moyaiSurfaceCavity;
      `).replace('#include <begin_vertex>', `
        #include <begin_vertex>
        moyaiSurfacePosition = position;
        moyaiSurfaceCavity = _moyai_cavity;
      `)
      shader.fragmentShader = shader.fragmentShader.replace('#include <common>', `
        #include <common>
        varying vec3 moyaiSurfacePosition;
        varying float moyaiSurfaceCavity;
        float moyaiHash(vec3 p) {
          p = fract(p * 0.1031);
          p += dot(p, p.yzx + 33.33);
          return fract((p.x + p.y) * p.z);
        }
        float moyaiNoise(vec3 p) {
          vec3 i = floor(p), f = fract(p);
          f = f * f * (3.0 - 2.0 * f);
          return mix(
            mix(mix(moyaiHash(i), moyaiHash(i + vec3(1,0,0)), f.x),
                mix(moyaiHash(i + vec3(0,1,0)), moyaiHash(i + vec3(1,1,0)), f.x), f.y),
            mix(mix(moyaiHash(i + vec3(0,0,1)), moyaiHash(i + vec3(1,0,1)), f.x),
                mix(moyaiHash(i + vec3(0,1,1)), moyaiHash(i + vec3(1,1,1)), f.x), f.y), f.z);
        }
        vec3 moyaiBumpNormal(vec3 surfacePosition, vec3 surfaceNormal, float height, float orientation) {
          vec3 dx = dFdx(surfacePosition), dy = dFdy(surfacePosition);
          vec3 r1 = cross(dy, surfaceNormal), r2 = cross(surfaceNormal, dx);
          float determinant = dot(dx, r1) * orientation;
          vec3 gradient = sign(determinant) * (dFdx(height) * r1 + dFdy(height) * r2);
          return normalize(abs(determinant) * surfaceNormal - gradient);
        }
      `).replace('#include <map_fragment>', `
        #include <map_fragment>
        float moyaiCavity = smoothstep(0.09, 0.68, moyaiSurfaceCavity);
        // Broad chips survive the small home-screen projection; fine grain fades
        // with the pixel footprint instead of turning into distant shimmer.
        float moyaiChips = moyaiNoise(moyaiSurfacePosition * 10.0);
        float moyaiPits = 1.0 - smoothstep(0.22, 0.40, moyaiNoise(moyaiSurfacePosition * 23.0));
        float moyaiGrain = moyaiNoise(moyaiSurfacePosition * 32.0);
        float moyaiPores = 1.0 - smoothstep(0.23, 0.39, moyaiNoise(moyaiSurfacePosition * 57.0));
        float moyaiFootprint = max(length(dFdx(moyaiSurfacePosition)), length(dFdy(moyaiSurfacePosition)));
        float moyaiFineDetail = 1.0 - smoothstep(0.025, 0.07, moyaiFootprint);
        vec3 moyaiCell = moyaiSurfacePosition * 40.0;
        float moyaiSeed = moyaiHash(floor(moyaiCell));
        float moyaiCrystal = 1.0 - smoothstep(0.10, 0.30, length(fract(moyaiCell) - 0.5));
        float moyaiGlitter = moyaiCrystal * step(0.91, moyaiSeed) * moyaiCavity;
        diffuseColor.rgb *= 0.68 + 0.42 * moyaiChips - 0.20 * moyaiPits
          + (0.08 * moyaiGrain - 0.08 * moyaiPores) * moyaiFineDetail;
      `).replace('#include <roughnessmap_fragment>', `
        #include <roughnessmap_fragment>
        roughnessFactor = mix(max(roughnessFactor, 0.84), 0.24, moyaiGlitter);
      `).replace('#include <metalnessmap_fragment>', `
        #include <metalnessmap_fragment>
        metalnessFactor = max(metalnessFactor, moyaiGlitter * 0.65);
      `).replace('#include <normal_fragment_maps>', `
        #include <normal_fragment_maps>
        float moyaiHeight = 0.024 * moyaiChips - 0.014 * moyaiPits
          + (0.0045 * moyaiGrain - 0.0045 * moyaiPores
          + 0.0015 * moyaiNoise(moyaiSurfacePosition * 110.0)) * moyaiFineDetail;
        normal = moyaiBumpNormal(-vViewPosition, normal, moyaiHeight, faceDirection);
      `).replace('#include <emissivemap_fragment>', `
        #include <emissivemap_fragment>
        vec3 moyaiMineralColor = mix(vec3(0.025, 0.09, 0.42), vec3(0.22, 0.025, 0.50),
          0.35 + moyaiNoise(moyaiSurfacePosition * 3.0) * 0.40);
        float moyaiCraterEdge = 1.0 - abs(dot(normal, normalize(vViewPosition)));
        totalEmissiveRadiance += moyaiMineralColor * moyaiCavity * (0.09 + 0.30 * pow(moyaiCraterEdge, 2.0));
        float moyaiGlintAngle = 0.2 + 0.8 * pow(max(dot(normal, normalize(vViewPosition)), 0.0), 12.0);
        totalEmissiveRadiance += vec3(0.36, 0.57, 1.0) * moyaiGlitter * moyaiGlintAngle * 2.2;
        // A directional grazing highlight traces the broken rear-left contour,
        // opposite the face in a three-quarter view, without a flat CSS halo.
        float moyaiRimFacing = smoothstep(0.05, 0.70,
          dot(normal, normalize(vec3(-0.8, 0.25, -0.55))));
        float moyaiRearLeft = 1.0 - smoothstep(-0.4, 1.3,
          moyaiSurfacePosition.x + moyaiSurfacePosition.z * 0.75);
        float moyaiRim = pow(moyaiCraterEdge, 1.85) * moyaiRimFacing * moyaiRearLeft;
        totalEmissiveRadiance += vec3(0.025, 0.19, 0.75) * moyaiRim * 1.25;
      `)
    }
    material.customProgramCacheKey = () => 'moyai-weathered-stone-v2'
  }
}
