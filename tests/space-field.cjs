const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app/static/space-field.js'), 'utf8');

function events() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name, callback) {
      if (listeners.get(name) === callback) listeners.delete(name);
    },
    emit(name) { listeners.get(name)?.(); },
  };
}

function setup({ reduced = false, hidden = false, fonts } = {}) {
  let draws = 0;
  let translations = [];
  let scale = [];
  let nextFrame = 0;
  const frames = new Map();
  const observers = [];
  const document = { ...events(), hidden, fonts };
  const motion = { ...events(), matches: reduced };
  const window = events();
  const context = new Proxy({
    clearRect() { draws += 1; translations = []; },
    translate(x, y) { translations.push([x, y]); },
    scale(x, y) { scale = [x, y]; },
    createRadialGradient() { return { addColorStop() {} }; },
  }, { get(target, key) { return target[key] ?? (() => {}); } });
  const canvas = {
    isConnected: true,
    getContext() { return context; },
    getBoundingClientRect() { return { left: 240, top: 50, width: 1200, height: 900 }; },
    setAttribute() {},
  };
  const sandbox = vm.createContext({
    ...window,
    document,
    devicePixelRatio: 3,
    matchMedia: () => motion,
    requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); },
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; this.elements = new Set(); observers.push(this); }
      observe(element) { this.elements.add(element); }
      disconnect() { this.disconnected = true; }
    },
  });
  vm.runInContext(source, sandbox);
  return {
    canvas, document, motion, window, frames, observers,
    mount: (options) => sandbox.MoyaiSpace.mount(canvas, options),
    draws: () => draws,
    hub: () => [translations[0][0] + 963 * scale[0], translations[0][1] + 493 * scale[1]],
    tick(time) {
      const [id, callback] = frames.entries().next().value;
      frames.delete(id);
      callback(time);
    },
  };
}

test('renders immediately at capped pixel density and throttles drawing to 30fps', () => {
  const scene = setup();
  const dispose = scene.mount();
  assert.equal(scene.draws(), 1);
  assert.equal(scene.canvas.width, 2400);
  assert.equal(scene.canvas.height, 1800);
  scene.tick(0);
  scene.tick(16);
  scene.tick(32);
  assert.equal(scene.draws(), 1);
  scene.tick(48);
  assert.equal(scene.draws(), 2);
  dispose();
  assert.equal(scene.frames.size, 0);
  assert.equal(scene.observers[0].disconnected, true);
  assert.equal(scene.document.listeners.size, 0);
  assert.equal(scene.motion.listeners.size, 0);
  assert.equal(scene.window.listeners.size, 0);
  dispose();
});

test('hidden tabs pause animation and resume without advancing through hidden time', () => {
  const scene = setup();
  const dispose = scene.mount();
  scene.document.hidden = true;
  scene.document.emit('visibilitychange');
  assert.equal(scene.frames.size, 0);
  scene.observers[0].callback();
  assert.equal(scene.draws(), 1);
  scene.document.hidden = false;
  scene.document.emit('visibilitychange');
  assert.equal(scene.frames.size, 1);
  assert.equal(scene.draws(), 2);
  scene.tick(1000000);
  assert.equal(scene.draws(), 2);
  dispose();
});

test('reduced motion stays static, redraws on resize, and reacts to preference changes', () => {
  const scene = setup({ reduced: true });
  const dispose = scene.mount();
  assert.equal(scene.draws(), 1);
  assert.equal(scene.frames.size, 0);
  scene.observers[0].callback();
  assert.equal(scene.draws(), 2);
  scene.motion.matches = false;
  scene.motion.emit('change');
  assert.equal(scene.frames.size, 1);
  scene.motion.matches = true;
  scene.motion.emit('change');
  assert.equal(scene.frames.size, 0);
  dispose();
});

test('remount and detached canvases release the previous lifecycle', () => {
  const scene = setup();
  scene.mount();
  scene.mount();
  assert.equal(scene.observers[0].disconnected, true);
  assert.equal(scene.frames.size, 1);
  scene.canvas.isConnected = false;
  scene.tick(0);
  assert.equal(scene.frames.size, 0);
  assert.equal(scene.observers[1].disconnected, true);
  assert.equal(scene.document.listeners.size, 0);
});

test('anchors the convergence point to the mark and tracks ancestor layout changes', () => {
  const scene = setup({ reduced: true });
  const container = {};
  const section = { parentElement: container };
  const bounds = { left: 790, top: 250, width: 100, height: 100 };
  const anchor = { parentElement: section, getBoundingClientRect: () => bounds };
  scene.canvas.parentElement = container;
  const dispose = scene.mount({ anchor });
  assert.deepEqual(scene.hub(), [600, 250]);
  assert.equal(scene.observers[0].elements.size, 4);
  assert.equal(scene.observers[0].elements.has(section), true);
  bounds.top += 24;
  scene.observers[0].callback();
  assert.deepEqual(scene.hub(), [600, 274]);
  assert.equal(scene.frames.size, 0);
  dispose();
});

test('late fonts realign the hub but do not redraw a disposed scene', async () => {
  let settleFonts;
  const fonts = { ...events(), ready: new Promise((resolve) => { settleFonts = resolve; }) };
  const scene = setup({ reduced: true, fonts });
  const bounds = { left: 790, top: 250, width: 100, height: 100 };
  const anchor = { getBoundingClientRect: () => bounds };
  const dispose = scene.mount({ anchor });
  bounds.top += 12;
  settleFonts();
  await fonts.ready;
  assert.deepEqual(scene.hub(), [600, 262]);
  dispose();
  assert.equal(fonts.listeners.size, 0);

  let settleAfterDispose;
  const pendingFonts = { ...events(), ready: new Promise((resolve) => { settleAfterDispose = resolve; }) };
  const detachedScene = setup({ reduced: true, fonts: pendingFonts });
  detachedScene.mount({ anchor })();
  anchor.getBoundingClientRect = () => { throw new Error('Disposed anchor measured'); };
  settleAfterDispose();
  await pendingFonts.ready;
  assert.equal(detachedScene.draws(), 1);
});
