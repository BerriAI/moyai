import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'

/** The cratered meteor, with a bounded render lifecycle owned by the home view. */
export function mountSculpture(host: HTMLElement | null) {
  if (!host) return () => {}
  let disposed = false, frame = 0, dragging = false, pointer = -1
  const initialYaw = 0, initialPitch = 0
  let lastX = 0, lastY = 0, yaw = initialYaw, pitch = initialPitch, velocity = 0
  let renderer: THREE.WebGLRenderer
  try { renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true }) }
  catch { return () => {} }
  const canvas = renderer.domElement
  canvas.tabIndex = 0
  canvas.setAttribute('role', 'img')
  canvas.setAttribute('aria-label', '3D Moyai. Drag to rotate, use arrow keys, or press Home to reset.')
  canvas.title = 'Drag to rotate · Arrow keys to turn · Home to reset'
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setClearColor(0, 0)
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.10
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = THREE.PCFSoftShadowMap
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(32, 1, .1, 30)
  const additionalTextures = new Set<THREE.Texture>()
  const studio = new RoomEnvironment(), pmrem = new THREE.PMREMGenerator(renderer)
  const environment = pmrem.fromScene(studio, .04)
  studio.dispose(); pmrem.dispose()
  scene.environment = environment.texture
  scene.environmentIntensity = .16
  // Keep the generator's reference projection aligned with the initial view.
  camera.position.set(0, 0, 8.6)
  camera.lookAt(0, 0, 0)
  scene.add(new THREE.HemisphereLight(0xb2bed5, 0x161820, .45))
  for (const [color, power, x, y, z] of [[0xf0f2f8, 3.8, 3, 5, 5], [0x7a9be6, 2.4, -4, 0, -2], [0x9b86d8, .95, 3, 1, -3], [0x829ee8, .9, -4, -2, 3]]) {
    const light = new THREE.DirectionalLight(color, power)
    light.position.set(x, y, z); scene.add(light)
    if (y === 5 || y === -2) {
      light.castShadow = true
      light.shadow.mapSize.set(1024, 1024)
      Object.assign(light.shadow.camera, { left: -3, right: 3, top: 3, bottom: -3, near: .1, far: 20 })
      light.shadow.camera.updateProjectionMatrix()
      light.shadow.normalBias = .015
      light.shadow.bias = -.0005
    }
  }
  const pivot = new THREE.Group(); scene.add(pivot)
  const motion = matchMedia('(prefers-reduced-motion: reduce)')
  let visible = true, ready = false
  function draw() {
    frame = 0
    if (disposed || !host?.isConnected) { cleanup(); return }
    if (!ready || !visible || document.hidden) return
    if (!dragging && !motion.matches && Math.abs(velocity) > .0001) { yaw += velocity; velocity *= .93 }
    pivot.rotation.set(pitch, yaw, 0)
    renderer.render(scene, camera)
    if (!dragging && !motion.matches && Math.abs(velocity) > .0001) schedule()
  }
  function schedule() { if (!disposed && !frame) frame = requestAnimationFrame(draw) }
  function resize() {
    const size = canvas.getBoundingClientRect()
    if (size.width && size.height) { renderer.setSize(size.width, size.height, false); camera.aspect = size.width / size.height; camera.updateProjectionMatrix(); schedule() }
  }
  function down(e: PointerEvent) {
    if (!e.isPrimary || e.button !== 0) return
    dragging = true; pointer = e.pointerId; lastX = e.clientX; lastY = e.clientY; velocity = 0
    canvas.setPointerCapture(pointer); canvas.classList.add('dragging')
  }
  function move(e: PointerEvent) {
    if (!dragging || e.pointerId !== pointer) return
    velocity = (e.clientX - lastX) * .012; yaw += velocity
    pitch = Math.max(-.65, Math.min(.65, pitch + (e.clientY - lastY) * .009))
    lastX = e.clientX; lastY = e.clientY; schedule()
  }
  function up() { dragging = false; pointer = -1; canvas.classList.remove('dragging'); schedule() }
  function key(e: KeyboardEvent) {
    if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home'].includes(e.key)) return
    e.preventDefault(); velocity = 0
    if (e.key === 'Home') { yaw = initialYaw; pitch = initialPitch }
    else if (e.key === 'ArrowLeft') yaw -= .18
    else if (e.key === 'ArrowRight') yaw += .18
    else pitch = Math.max(-.65, Math.min(.65, pitch + (e.key === 'ArrowUp' ? -.12 : .12)))
    schedule()
  }
  function visibility() { velocity = 0; schedule() }
  function release(object: THREE.Object3D) {
    const textures = new Set<THREE.Texture>()
    const geometries = new Set<THREE.BufferGeometry>(), materials = new Set<THREE.Material>()
    object.traverse(child => {
      const mesh = child as THREE.Mesh
      if (mesh.geometry) geometries.add(mesh.geometry)
      if (mesh.material) for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
        materials.add(material)
        if (material instanceof THREE.MeshStandardMaterial) {
          for (const texture of [material.map, material.normalMap, material.roughnessMap, material.metalnessMap]) {
            if (texture) textures.add(texture)
          }
        } else if (material instanceof THREE.MeshBasicMaterial && material.map) textures.add(material.map)
      }
      if (child instanceof THREE.DirectionalLight) child.shadow.dispose()
    })
    textures.forEach(texture => texture.dispose())
    materials.forEach(material => material.dispose())
    geometries.forEach(geometry => geometry.dispose())
  }
  function cleanup() {
    if (disposed) return
    disposed = true; cancelAnimationFrame(frame); observer.disconnect(); resizeObserver.disconnect()
    document.removeEventListener('visibilitychange', visibility); motion.removeEventListener('change', visibility)
    canvas.removeEventListener('pointerdown', down); canvas.removeEventListener('pointermove', move)
    canvas.removeEventListener('pointerup', up); canvas.removeEventListener('lostpointercapture', up)
    canvas.removeEventListener('pointercancel', up); canvas.removeEventListener('keydown', key)
    canvas.removeEventListener('webglcontextlost', lost)
    release(scene); additionalTextures.forEach(texture => texture.dispose())
    environment.dispose(); renderer.dispose(); canvas.remove(); host?.classList.remove('sculpture-ready')
  }
  function lost(e: Event) { e.preventDefault(); cleanup() }
  const observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; schedule() })
  const resizeObserver = new ResizeObserver(resize)
  observer.observe(host); resizeObserver.observe(host)
  canvas.addEventListener('pointerdown', down); canvas.addEventListener('pointermove', move)
  canvas.addEventListener('pointerup', up); canvas.addEventListener('lostpointercapture', up)
  canvas.addEventListener('pointercancel', up); canvas.addEventListener('keydown', key)
  canvas.addEventListener('webglcontextlost', lost)
  document.addEventListener('visibilitychange', visibility); motion.addEventListener('change', visibility)
  // GLB keeps geometry in the same-origin response; data: buffer fetches violate our CSP.
  const loader = new GLTFLoader()
  loader.register(parser => {
    // Embedded maps are images, so load through img-src rather than fetching
    // blob URLs via ImageBitmapLoader, which this app's connect-src disallows.
    parser.textureLoader = new THREE.TextureLoader()
    return { name: 'MoyaiEmbeddedImages' }
  })
  loader.load('/static/models/moyai.glb', gltf => {
    if (disposed || !host.isConnected) { release(gltf.scene); cleanup(); return }
    const meshes: THREE.Mesh[] = []
    gltf.scene.traverse(child => { if (child instanceof THREE.Mesh) meshes.push(child) })
    const reference = meshes.find(mesh => mesh.material instanceof THREE.MeshBasicMaterial && mesh.material.name === 'Reference surface')?.material
    const stone = meshes.find(mesh => mesh.material instanceof THREE.MeshBasicMaterial && mesh.material.name === 'Inferred back')?.material
    if (reference instanceof THREE.MeshBasicMaterial && reference.map && stone instanceof THREE.MeshBasicMaterial && stone.map) {
      const stoneMap = stone.map
      additionalTextures.add(stoneMap)
      stoneMap.wrapS = stoneMap.wrapT = THREE.RepeatWrapping
      reference.onBeforeCompile = shader => {
        shader.uniforms.moyaiStoneMap = { value: stoneMap }
        shader.vertexShader = shader.vertexShader.replace('#include <common>', `
          #include <common>
          varying vec3 moyaiPosition;
          varying vec3 moyaiNormal;
        `).replace('#include <begin_vertex>', `
          #include <begin_vertex>
          moyaiPosition = position;
          moyaiNormal = normal;
        `)
        shader.fragmentShader = shader.fragmentShader.replace('#include <common>', `
          #include <common>
          uniform sampler2D moyaiStoneMap;
          varying vec3 moyaiPosition;
          varying vec3 moyaiNormal;
        `).replace('#include <map_fragment>', `
          #ifdef USE_MAP
            vec3 rockNormal = normalize(moyaiNormal);
            vec3 weights = pow(abs(rockNormal), vec3(4.0));
            weights /= max(weights.x + weights.y + weights.z, 0.0001);
            vec2 stoneScale = vec2(0.68, 0.44);
            vec3 stoneColor =
              texture2D(moyaiStoneMap, moyaiPosition.zy * stoneScale).rgb * weights.x +
              texture2D(moyaiStoneMap, moyaiPosition.xz * stoneScale).rgb * weights.y +
              texture2D(moyaiStoneMap, moyaiPosition.xy * stoneScale).rgb * weights.z;
            vec3 photoColor = texture2D(map, vMapUv).rgb;
            float referenceWeight = smoothstep(0.25, 0.60, rockNormal.z);
            diffuseColor.rgb *= mix(stoneColor, photoColor, referenceWeight);
          #endif
        `)
      }
      // Both halves share one continuous surface. Object-space projections avoid
      // stretching the front photograph over steep sides or a hard equator seam.
      for (const mesh of meshes) if (mesh.material === stone) mesh.material = reference
      stone.dispose()
    }
    let sheen: THREE.MeshPhysicalMaterial | undefined
    for (const mesh of meshes) {
      mesh.castShadow = true; mesh.receiveShadow = true
      if (!(mesh.material instanceof THREE.MeshBasicMaterial)) continue
      // Preserve the reference's photographed color and shadow. A separate,
      // faint specular layer responds to rotation without washing that out.
      mesh.material.toneMapped = false
      sheen ??= new THREE.MeshPhysicalMaterial({
        color: 0x000000, roughness: .64, metalness: 0, specularIntensity: .14,
        clearcoat: .18, clearcoatRoughness: .25, transparent: true, opacity: .22,
        blending: THREE.AdditiveBlending, depthWrite: false,
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
      })
      const surface = new THREE.Mesh(mesh.geometry, sheen)
      surface.name = 'Mineral sheen'; surface.receiveShadow = true; surface.renderOrder = 1
      mesh.add(surface)
    }
    pivot.add(gltf.scene); ready = true; host.append(canvas); host.classList.add('sculpture-ready'); resize()
  }, undefined, cleanup)
  return cleanup
}
