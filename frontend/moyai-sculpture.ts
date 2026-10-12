import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

/** The film's sculpture, with a bounded render lifecycle owned by the home view. */
export function mountSculpture(host: HTMLElement | null) {
  if (!host) return () => {}
  let disposed = false, frame = 0, dragging = false, pointer = -1
  let lastX = 0, lastY = 0, yaw = .35, pitch = .06, velocity = 0
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
  renderer.toneMappingExposure = 1.35
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(32, 1, .1, 30)
  camera.position.set(0, .15, 8.6)
  camera.lookAt(0, 0, 0)
  scene.add(new THREE.HemisphereLight(0xb8d8ff, 0x111020, 2))
  for (const [color, power, x, y, z] of [[0xe0edff, 7, -3, 4, 5], [0x8263ff, 9, 3, 1, -3], [0xa4d8ff, 6, -4, 1, -2]]) {
    const light = new THREE.DirectionalLight(color, power)
    light.position.set(x, y, z); scene.add(light)
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
    if (e.key === 'Home') { yaw = .35; pitch = .06 }
    else if (e.key === 'ArrowLeft') yaw -= .18
    else if (e.key === 'ArrowRight') yaw += .18
    else pitch = Math.max(-.65, Math.min(.65, pitch + (e.key === 'ArrowUp' ? -.12 : .12)))
    schedule()
  }
  function visibility() { velocity = 0; schedule() }
  function release(object: THREE.Object3D) {
    object.traverse(child => {
      const mesh = child as THREE.Mesh
      mesh.geometry?.dispose()
      if (mesh.material) for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) material.dispose()
    })
  }
  function cleanup() {
    if (disposed) return
    disposed = true; cancelAnimationFrame(frame); observer.disconnect(); resizeObserver.disconnect()
    document.removeEventListener('visibilitychange', visibility); motion.removeEventListener('change', visibility)
    canvas.removeEventListener('pointerdown', down); canvas.removeEventListener('pointermove', move)
    canvas.removeEventListener('pointerup', up); canvas.removeEventListener('lostpointercapture', up)
    canvas.removeEventListener('pointercancel', up); canvas.removeEventListener('keydown', key)
    canvas.removeEventListener('webglcontextlost', lost)
    release(scene); renderer.dispose(); canvas.remove(); host?.classList.remove('sculpture-ready')
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
  new GLTFLoader().load('/static/models/moyai.glb', gltf => {
    if (disposed || !host.isConnected) { release(gltf.scene); cleanup(); return }
    pivot.add(gltf.scene); ready = true; host.append(canvas); host.classList.add('sculpture-ready'); resize()
  }, undefined, cleanup)
  return cleanup
}
