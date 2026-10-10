/*
 * Moyai's decorative constellation, adapted from the approved Lens
 * AgentSwarmField and DeepSpaceBackdrop. Worker counts are artwork, not usage.
 * The original golden-angle geometry and packet motion remain deterministic.
 */
(() => {
  'use strict';

  const HUB = { x: 963, y: 493 };
  const ANCHORS = [{ x: 747, y: 641 }, { x: 1210, y: 651 }, { x: 972, y: 853 }];
  const CLUSTERS = [
    ...ANCHORS,
    { x: 380, y: 824 }, { x: 567, y: 938 }, { x: 750, y: 971 },
    { x: 1191, y: 956 }, { x: 1400, y: 914 }, { x: 1593, y: 794 },
  ];
  const TAU = Math.PI * 2;
  const FRAME_DURATION = 1000 / 30;
  const NOOP = () => {};
  const mounts = new WeakMap();

  const makeStars = (count) => Array.from({ length: count }, (_, index) => {
    const depth = .5 + .5 * Math.sin(index * 1.371);
    const bright = index % 17 === 0;
    return {
      index,
      angle: index * 2.39996323,
      radius: Math.sqrt((index + .5) / count),
      depth,
      bright,
      size: bright ? 2.8 + depth : .75 + depth * 1.35,
      color: index % 19 === 0 ? '#f8d797' : index % 6 === 0 ? '#d4cbff' : '#d7f0ff',
    };
  });
  const backgroundStars = makeStars(880);
  const swarmStars = makeStars(740);

  const pointBetween = (source, target, progress, bend) => ({
    x: source.x + (target.x - source.x) * progress + Math.sin(progress * Math.PI) * bend,
    y: source.y + (target.y - source.y) * progress,
  });

  function circle(context, x, y, radius, color, opacity) {
    context.globalAlpha = opacity;
    context.fillStyle = color;
    context.beginPath();
    context.arc(x, y, radius, 0, TAU);
    context.fill();
  }

  // Fade only the outer upper stars, leaving the hub and coordinated workers
  // luminous like the original film. The composer supplies its own backdrop.
  function spatialOpacity(y) {
    return Math.max(.22, Math.min(1, (y - 240) / 280));
  }

  function drawStars(context, stars, frame, background, strength = 1) {
    for (const star of stars) {
      const { index, radius, depth, size, color, bright } = star;
      const angle = star.angle + frame * (background ? .0011 : .0014);
      const x = 960 + Math.cos(angle) * (background ? 1040 : 920) * radius
        + Math.sin(index * .21 + frame * (background ? .012 : .014)) * (7 + depth * 8);
      const y = (background ? 720 : 738) + Math.sin(angle) * (background ? 490 : 355) * radius
        + Math.cos(index * .17 + frame * .011) * 25;
      const flicker = .55 + .45 * Math.sin(frame / 24 + index) ** 2;
      const opacity = (bright ? .9 : .25 + depth * .55) * flicker
        * spatialOpacity(y) * (background ? .4 : .95) * strength;

      if (bright) {
        circle(context, x, y, size * 11, color, opacity * .025);
        circle(context, x, y, size * 4.5, color, opacity * .06);
        context.globalAlpha = opacity * .3;
        context.strokeStyle = color;
        context.lineWidth = .65;
        context.beginPath();
        context.moveTo(x - size * 4, y);
        context.lineTo(x + size * 4, y);
        context.moveTo(x, y - size * 4);
        context.lineTo(x, y + size * 4);
        context.stroke();
      }
      circle(context, x, y, size, color, opacity);
    }
  }

  function drawClusters(context, frame) {
    CLUSTERS.forEach((cluster, index) => {
      const important = index < 3;
      const color = important && frame > 48 + index * 21 ? '#ffdb94' : index % 2 ? '#8fd9e5' : '#b0a6ee';
      const target = important ? HUB : ANCHORS[(index - 3) % 3];
      const active = .55 + .45 * Math.sin(frame / 12 - index) ** 2;
      const shifted = {
        x: cluster.x + Math.sin(frame / 38 + index * 2) * 8,
        y: cluster.y + Math.cos(frame / 43 + index) * 7,
      };
      const opacity = (important ? .95 : .58) * spatialOpacity(shifted.y);

      context.globalAlpha = opacity * .19;
      context.strokeStyle = color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(shifted.x, shifted.y);
      context.quadraticCurveTo(
        (shifted.x + target.x) / 2 + (index % 2 ? 38 : -38),
        (shifted.y + target.y) / 2,
        target.x, target.y,
      );
      context.stroke();

      for (let packet = 0; packet < 4; packet += 1) {
        const progress = (frame * .014 + packet * .25 + index * .17) % 1;
        const point = pointBetween(shifted, target, progress, index % 2 ? 22 : -22);
        context.fillStyle = color;
        context.globalAlpha = opacity * Math.sin(progress * Math.PI);
        context.fillRect(point.x - 2, point.y - 2, 4, 4);
        context.globalAlpha *= .3;
        context.beginPath();
        context.moveTo(point.x - 7, point.y + 11);
        context.lineTo(point.x - 3, point.y + 5);
        context.stroke();
      }

      for (let worker = 0; worker < (important ? 19 : 11); worker += 1) {
        const angle = worker * 2.39996 + Math.sin(frame / 45 + index) * .11;
        const radius = Math.sqrt(worker) * (important ? 12 : 10);
        const x = shifted.x + Math.cos(angle) * radius;
        const y = shifted.y + Math.sin(angle) * radius * .77;
        const processing = (Math.floor(frame / 6) + worker + index * 3) % 13 < 3;
        if (processing) {
          circle(context, x, y, 20, color, opacity * .035);
          circle(context, x, y, 10, color, opacity * .065);
          context.globalAlpha = opacity * active * .45;
          context.strokeStyle = color;
          context.lineWidth = .6;
          context.beginPath();
          context.moveTo(x - 9, y - 3);
          context.lineTo(x - 9, y - 9);
          context.lineTo(x - 3, y - 9);
          context.moveTo(x + 9, y + 3);
          context.lineTo(x + 9, y + 9);
          context.lineTo(x + 3, y + 9);
          context.stroke();
        }
        circle(context, x, y, processing ? 4 : 2.4, processing ? '#fff5d5' : '#adc8e2', opacity * (processing ? 1 : .62));
      }

      for (let packet = 0; packet < 16; packet += 1) {
        const source = { x: index % 2 ? 2040 : -120, y: 460 + ((packet * 137 + index * 93) % 610) };
        const progress = (frame * .006 + packet / 16 + index * .19) % 1;
        const point = pointBetween(source, shifted, progress, Math.sin(index) * 80);
        context.globalAlpha = opacity * Math.sin(progress * Math.PI) * .46;
        context.fillStyle = index % 2 ? '#8392bd' : '#7bb9c9';
        context.fillRect(point.x, point.y, progress > .8 ? 4 : 9, 3);
      }
    });
  }

  function mount(canvas, { anchor, animate = true } = {}) {
    if (!canvas || typeof canvas.getContext !== 'function') return NOOP;
    mounts.get(canvas)?.();
    const context = canvas.getContext('2d', { alpha: true });
    if (!context) return NOOP;

    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    let disposed = false;
    let animation = null;
    let previousTime = null;
    let elapsed = 0;
    let width = 0;
    let height = 0;
    let pixelRatio = 1;
    let scale = 1;
    let originX = 0;
    let originY = 0;
    let lowerGap = 0;
    canvas.setAttribute('aria-hidden', 'true');

    function draw() {
      if (!width || !height || disposed) return;
      const frame = 132 + elapsed * 30 / 1000;
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.save();
      context.translate(originX, originY);
      context.scale(scale, scale);

      const bloom = context.createRadialGradient(970, 773, 0, 970, 773, 860);
      bloom.addColorStop(0, '#26466a38');
      bloom.addColorStop(.65, '#14213b0f');
      bloom.addColorStop(1, '#07132100');
      context.globalAlpha = 1;
      context.fillStyle = bloom;
      context.fillRect(-120, 160, 2160, 1100);
      if (lowerGap > 60) {
        // Anchoring the hub higher can reveal extra space beneath the original
        // composition. Extend its own golden-angle field, never a random layer.
        context.save();
        context.translate(0, lowerGap / scale);
        drawStars(context, backgroundStars, frame + 420, true, .45);
        context.restore();
      }
      drawStars(context, backgroundStars, frame, true);
      drawStars(context, swarmStars, frame, false);
      drawClusters(context, frame);
      context.restore();
    }

    function cancel() {
      if (animation !== null) cancelAnimationFrame(animation);
      animation = null;
      previousTime = null;
    }

    function tick(time) {
      animation = null;
      if (disposed || document.hidden || !animate || reducedMotion.matches) return;
      if (!canvas.isConnected) {
        cleanup();
        return;
      }
      if (previousTime === null) previousTime = time;
      const difference = time - previousTime;
      if (difference >= FRAME_DURATION) {
        elapsed += Math.min(difference, 100);
        previousTime = time;
        draw();
      }
      animation = requestAnimationFrame(tick);
    }

    function resume() {
      cancel();
      if (disposed || document.hidden) return;
      draw();
      if (animate && !reducedMotion.matches) animation = requestAnimationFrame(tick);
    }

    function resize() {
      if (disposed) return;
      const bounds = canvas.getBoundingClientRect();
      width = Math.max(0, bounds.width);
      height = Math.max(0, bounds.height);
      pixelRatio = Math.min(2, globalThis.devicePixelRatio || 1);
      // Preserve the source geometry. Small screens crop the outer clusters
      // instead of compressing every star and worker into a tiny point.
      scale = Math.max(width / 1920, Math.min(.6, height / 1250));
      originX = (width - 1920 * scale) / 2;
      originY = height - 1024 * scale;
      const anchorBounds = anchor?.isConnected !== false && anchor?.getBoundingClientRect();
      if (anchorBounds?.width > 0 && anchorBounds.height > 0) {
        originX = anchorBounds.left + anchorBounds.width / 2 - bounds.left - HUB.x * scale;
        originY = anchorBounds.top + anchorBounds.height / 2 - bounds.top - HUB.y * scale;
      }
      lowerGap = Math.max(0, height - (originY + 1160 * scale));
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      if (!document.hidden) draw();
    }

    const observer = new ResizeObserver(resize);
    function cleanup() {
      if (disposed) return;
      disposed = true;
      cancel();
      observer.disconnect();
      document.removeEventListener('visibilitychange', resume);
      reducedMotion.removeEventListener('change', resume);
      globalThis.removeEventListener('resize', resize);
      document.fonts?.removeEventListener('loadingdone', resize);
      mounts.delete(canvas);
    }

    const observedElements = new Set([canvas]);
    if (canvas.parentElement) observedElements.add(canvas.parentElement);
    for (let element = anchor; element; element = element.parentElement) {
      observedElements.add(element);
      if (element === canvas.parentElement) break;
    }
    observedElements.forEach((element) => observer.observe(element));
    document.addEventListener('visibilitychange', resume);
    reducedMotion.addEventListener('change', resume);
    globalThis.addEventListener('resize', resize);
    document.fonts?.addEventListener('loadingdone', resize);
    // A late font can move the mark without changing its own fixed dimensions.
    // resize's disposal guard also fences a promise settled after navigation.
    document.fonts?.ready.then(resize);
    mounts.set(canvas, cleanup);
    resize();
    if (animate && !document.hidden && !reducedMotion.matches) animation = requestAnimationFrame(tick);
    return cleanup;
  }

  globalThis.MoyaiSpace = { mount };
})();
