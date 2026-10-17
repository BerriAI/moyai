import * as THREE from 'three'

/** Keep the original frontal image while revealing the supplied solid geometry on rotation. */
export function finishOriginalSurface(mesh: THREE.Mesh) {
  if (!(mesh.material instanceof THREE.MeshStandardMaterial) || !mesh.material.emissiveMap) return
  const projection = mesh.geometry.getAttribute('uv1')
  if (!projection) return
  const material = mesh.material, photograph = material.emissiveMap
  material.emissiveMap = null
  material.normalScale.set(1.2, 1.2)
  material.alphaTest = .5
  mesh.geometry.setAttribute('originalUv', projection)
  material.onBeforeCompile = shader => {
    shader.uniforms.originalPhotograph = { value: photograph }
    shader.vertexShader = shader.vertexShader.replace('#include <common>', `
      #include <common>
      attribute vec2 originalUv;
      varying vec2 originalPhotoUv;
      varying float originalFrontView;
    `).replace('#include <begin_vertex>', `
      #include <begin_vertex>
      originalPhotoUv = originalUv;
      originalFrontView = normalize(mat3(modelViewMatrix) * vec3(0, 0, 1)).z;
    `)
    shader.fragmentShader = shader.fragmentShader.replace('#include <common>', `
      #include <common>
      uniform sampler2D originalPhotograph;
      varying vec2 originalPhotoUv;
      varying float originalFrontView;
    `).replace('#include <map_fragment>', `
      #include <map_fragment>
      float originalWeight = smoothstep(0.78, 0.995, originalFrontView);
      diffuseColor.a *= mix(1.0, texture2D(originalPhotograph, originalPhotoUv).a, originalWeight);
    `)
  }
  material.customProgramCacheKey = () => 'moyai-original-profile-body-v1'
  const reference = new THREE.MeshBasicMaterial({
    map: photograph, transparent: true, depthWrite: false, toneMapped: false,
    polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  })
  reference.onBeforeCompile = shader => {
    shader.vertexShader = shader.vertexShader.replace('#include <common>', `
      #include <common>
      varying float originalFrontView;
    `).replace('#include <begin_vertex>', `
      #include <begin_vertex>
      originalFrontView = normalize(mat3(modelViewMatrix) * vec3(0, 0, 1)).z;
    `)
    shader.fragmentShader = shader.fragmentShader.replace('#include <common>', `
      #include <common>
      varying float originalFrontView;
    `).replace('#include <map_fragment>', `
      #include <map_fragment>
      diffuseColor.a *= smoothstep(0.78, 0.995, originalFrontView);
    `)
  }
  reference.customProgramCacheKey = () => 'moyai-original-profile-photograph-v1'
  const overlay = new THREE.Mesh(mesh.geometry, reference)
  overlay.name = 'Original front appearance'
  overlay.renderOrder = 1
  mesh.add(overlay)
}
