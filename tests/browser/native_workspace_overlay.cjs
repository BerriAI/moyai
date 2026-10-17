// Real React/Radix/workspace components in an isolated Vite dev fixture.
// No static build, production app restart, external service, or provider call.
// This supplements the parent-owned actual-application before/after proof.
const { before, after, test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const os = require('node:os');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const entry = path.join(root, 'tests/__workspace_overlay_fixture.tsx');
const moduleUrl = '/tests/__workspace_overlay_fixture.tsx';
let server, browser, address, cache;

const fixture = `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button } from '@/components/ui/button';
import { WorkspaceOverlay, WorkspaceOverlayTrigger, WorkspaceOverlayContent } from '../native/web/workspace-overlay';
import { WorkspaceOverview } from '../native/web/workspace';
function Fixture() {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState(new URLSearchParams(location.search).get('panel') !== 'closed');
  const [expanded, setExpanded] = useState(false);
  const [size, setSize] = useState('65%');
  const [computerClicks, setComputerClicks] = useState(0);
  window.overlayFixture = { setSize, setPanel, setExpanded, setOpen };
  return <div className="conversation">
    <WorkspaceOverlay open={open} onOpenChange={setOpen} panelVisible={panel} panelExpanded={expanded}>
      <div className={'workspace ' + (panel ? 'with-panel' : 'panel-closed') + (expanded ? ' panel-expanded' : '')}
        style={{ '--chat-size': size }}>
        <header className="topbar">
          <WorkspaceOverlayTrigger asChild><Button type="button">Workspace</Button></WorkspaceOverlayTrigger>
        </header>
        <section className="chat"><div className="chat-layout">
        <div className="chat-content"><button type="button">Chat outside</button></div>
        <WorkspaceOverlayContent>
          <WorkspaceOverview id="overlay-fixture" pullRequests={[]} onUpdated={() => {}}
            onPullRequest={() => setOpen(false)} onSubagent={() => setOpen(false)} onOutput={() => setOpen(false)} />
        </WorkspaceOverlayContent>
        </div></section>
        {panel && <><div className="divider" /><aside className="details-panel">
          <button type="button" onClick={() => setComputerClicks(n => n + 1)}>Computer outside</button>
          <output aria-label="Computer clicks">{computerClicks}</output>
        </aside></>}
      </div>
    </WorkspaceOverlay>
  </div>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`;

const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>Isolated workspace overlay regression</title>
<style>
  :root { color-scheme: dark; --ui-border:#303442; --ui-control-border:#667080;
    --ui-subtle:#151924; --ui-foreground:#eeeef2; --ui-primary:#a8d7f8; --ui-muted:#a8b0c0;
    --ui-secondary:#232a38; }
  * { box-sizing:border-box; } body { margin:0; padding:16px; font:14px/1.4 system-ui; }
  button,input { font:inherit; padding:8px; } button { cursor:pointer; }
  .conversation { height:calc(100dvh - 32px); }
  .workspace { position:relative; display:grid; height:100%;
    grid-template-rows:48px minmax(0,1fr); grid-template-columns:minmax(0,var(--chat-size)) 8px minmax(0,1fr); }
  .topbar { grid-column:1; grid-row:1; display:flex; align-items:center; justify-content:end; padding:4px 8px; }
  .chat { grid-column:1; grid-row:2; min-width:0; background:#10141e; }
  .divider { grid-column:2; grid-row:1/-1; background:#555; }
  .details-panel { grid-column:3; grid-row:1/-1; min-width:0; background:#222a38; padding:16px; }
  .workspace.panel-closed,.workspace.panel-expanded { grid-template-columns:minmax(0,1fr); }
  .workspace.panel-expanded > :is(.topbar,.chat,.divider) { display:none; }
  .workspace.panel-expanded > .details-panel { grid-column:1; }
  [data-slot="dialog-overlay"] { position:fixed; inset:0; z-index:1000; background:#0008; }
  [data-slot="dialog-content"] { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
    padding:24px; z-index:1001; width:320px; background:var(--ui-subtle); }
  @media(max-width:1000px) {
    .workspace { grid-template-columns:minmax(0,1fr); }
    .workspace.with-panel > :is(.topbar,.chat) { visibility:hidden; pointer-events:none; }
    .divider { display:none; } .details-panel { position:absolute; inset:0; z-index:30; }
  }
</style><div id="root"></div><script type="module" src="${moduleUrl}"></script></html>`;

before(async () => {
  const { createServer } = await import('vite');
  cache = await fs.mkdtemp(path.join(os.tmpdir(), 'moyai-overlay-vite-'));
  server = await createServer({
    configFile: false, root, cacheDir: cache, logLevel: 'error',
    resolve: { alias: { '@': path.join(root, 'frontend') } },
    server: { host: '127.0.0.1', port: 0, strictPort: false },
    plugins: [{
      name: 'isolated-workspace-overlay-regression',
      resolveId(id) { if (id === moduleUrl) return entry; },
      load(id) { if (id === entry) return fixture; },
      configureServer(vite) {
        vite.middlewares.use((request, response, next) => {
          const url = new URL(request.url, 'http://localhost');
          if (url.pathname === '/') {
            void vite.transformIndexHtml(url.pathname, html).then(document => {
              response.setHeader('Content-Type', 'text/html'); response.end(document);
            }).catch(next);
            return;
          }
          if (url.pathname === '/api/sessions/overlay-fixture/workspace') {
            response.setHeader('Content-Type', 'application/json');
            response.end(JSON.stringify({
              links: [{ kind: 'pull_request', title: 'Example change', url: 'https://github.com/example/repository/pull/1',
                origin: 'attached', can_remove: false }],
              files: Array.from({ length: 24 }, (_, i) => ({
                id: String(i), name: `Example output ${i}`, size: 100, url: `/files/${i}`, media_type: 'text/plain',
              })),
              sources: [], subagents: [{ id: 'example:one', parent_id: '', name: 'Example agent', avatar: 0,
                status: 'running', can_inspect: true, started_at: '', depth: 0 }], subagents_limited: false,
            })); return;
          }
          next();
        });
      },
    }],
  });
  await server.listen();
  // This virtual entry cannot be found by Vite's file-system dependency scan.
  // Finish its cold import discovery before testing layout and focus behavior.
  await server.transformRequest(moduleUrl);
  await server.waitForRequestsIdle();
  address = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  await browser?.close();
  await server?.close();
  if (cache) await fs.rm(cache, { recursive: true, force: true });
});

async function pageFor(t, options = {}, query = '') {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, ...options });
  const errors = [];
  const loadingErrors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('requestfailed', request => loadingErrors.push(`${request.url()}: ${request.failure()?.errorText}`));
  page.on('console', message => { if (message.type() === 'error') loadingErrors.push(message.text()); });
  await page.route('**/*', route =>
    new URL(route.request().url()).origin === address ? route.continue() : route.abort());
  t.after(async () => { await page.close(); assert.deepEqual(errors, [], 'No browser runtime errors'); });
  await page.goto(address + '/' + query);
  await page.getByRole('button', { name: 'Workspace', exact: true }).waitFor().catch(error => {
    throw new Error(`Workspace fixture failed to load: ${loadingErrors.join('\n')}`, { cause: error });
  });
  return page;
}

async function opened(page) {
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  const content = page.getByRole('dialog', { name: 'Workspace resources', exact: true });
  await content.waitFor({ state: 'visible' });
  await content.locator('section').last().waitFor();
  await content.evaluate(element => Promise.all(element.getAnimations().map(animation => animation.finished)));
  return content;
}

async function geometry(page) {
  return page.evaluate(() => {
    const rect = selector => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width };
    };
    return { chat: rect('.chat'), body: rect('.chat-content'), host: rect('.workspace-overlay-host'), content: rect('.workspace-overlay-content'),
      workspace: rect('.workspace'), panel: document.querySelector('.details-panel') ? rect('.details-panel') : null };
  });
}

test('workspace floats within chat across live ratio changes without resizing it', async t => {
  const page = await pageFor(t);
  const content = await opened(page);
  for (const size of ['65%', '380px', '820px']) {
    await page.evaluate(size => window.overlayFixture.setSize(size), size);
    await page.waitForFunction(size => document.querySelector('.workspace').style.getPropertyValue('--chat-size') === size, size);
    const box = await geometry(page);
    assert(Math.abs(box.host.right - box.chat.right) < 1, JSON.stringify(box));
    assert(Math.abs(box.content.right - (box.chat.right - 8)) < 1);
    assert(box.content.width <= 360.1 && box.content.left >= box.chat.left);
    assert(box.content.bottom <= box.chat.bottom);
    assert(box.content.right < box.panel.left);
    assert(box.content.right < box.workspace.right - 100, 'Not anchored to workspace/viewport far right');
    assert.deepEqual(box.body, box.chat, 'The floating menu must not reserve chat space');
  }
  assert.deepEqual(await content.locator('section').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))),
    ['Pull requests', 'Agents', 'Outputs']);
  assert(await content.evaluate(element => element.scrollHeight > element.clientHeight), 'One contained scrolling overlay');
});

test('Radix retains exit animation, returns Escape focus, and does not animate resource refreshes', async t => {
  const page = await pageFor(t);
  await page.evaluate(() => {
    window.animations = [];
    for (const type of ['animationstart', 'animationend']) document.addEventListener(type, event => {
      if (event.target.classList.contains('workspace-overlay-content')) {
        window.animations.push({ type, name: event.animationName, connected: event.target.isConnected });
      }
    });
  });
  const content = await opened(page);
  assert(await content.evaluate(element => element.contains(document.activeElement)), 'Focus enters the dialog');
  await page.evaluate(() => window.overlayFixture.setSize('700px'));
  assert.equal((await page.evaluate(() => window.animations)).filter(item => item.type === 'animationstart').length, 1);
  await page.keyboard.press('Escape');
  await content.waitFor({ state: 'detached' });
  assert.equal(await page.getByRole('button', { name: 'Workspace', exact: true }).evaluate(element => element === document.activeElement), true);
  const events = await page.evaluate(() => window.animations);
  for (const name of ['workspace-overlay-enter', 'workspace-overlay-leave']) {
    assert(events.some(event => event.name === name && event.type === 'animationstart'));
    assert(events.some(event => event.name === name && event.type === 'animationend' && event.connected));
  }
});

test('outside dismissal remains nonmodal and the clicked computer control still works', async t => {
  const page = await pageFor(t);
  const content = await opened(page);
  await page.getByRole('button', { name: 'Computer outside', exact: true }).click();
  await content.waitFor({ state: 'detached' });
  assert.equal(await page.getByLabel('Computer clicks').textContent(), '1');
  assert.equal(await page.getByRole('button', { name: 'Computer outside', exact: true }).evaluate(element => element === document.activeElement), true);
});

test('existing attach-link dialog consumes Escape before the workspace overlay', async t => {
  const page = await pageFor(t);
  const content = await opened(page);
  const add = content.getByRole('button', { name: 'Add pull requests', exact: true });
  await add.click();
  const nested = page.getByRole('dialog', { name: 'Attach pull request', exact: true });
  await nested.waitFor({ state: 'visible' });
  await page.getByLabel('URL', { exact: true }).fill('https://github.com/example/repository/pull/2');
  await page.keyboard.press('Escape');
  await nested.waitFor({ state: 'detached' });
  assert.equal(await content.getAttribute('data-state'), 'open');
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Add pull requests');
  await page.keyboard.press('Escape');
  await content.waitFor({ state: 'detached' });
});

for (const width of [768, 320]) {
  test(`closed resource panel leaves overlay inside the ${width}px main chat`, async t => {
    const page = await pageFor(t, { viewport: { width, height: 800 } }, '?panel=closed');
    const before = await page.locator('.chat-content').boundingBox();
    await opened(page);
    const box = await geometry(page);
    assert(box.content.left >= box.chat.left && box.content.right <= box.chat.right);
    assert(box.content.bottom <= box.chat.bottom && box.content.width <= 360.1);
    assert.deepEqual(await page.locator('.chat-content').boundingBox(), before, 'Compact menu must not resize chat');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  });
}

test('resource panel taking over chat closes the overlay without reopening after resize', async t => {
  const page = await pageFor(t);
  const content = await opened(page);
  await page.setViewportSize({ width: 768, height: 800 });
  // Visibility hides the exit frame immediately; wait for Radix to actually
  // unmount it, not merely disappear from the accessibility role query.
  await page.locator('[data-slot="workspace-overlay-content"]').waitFor({ state: 'detached' });
  await page.setViewportSize({ width: 1440, height: 900 });
  assert.equal(await page.getByRole('button', { name: 'Workspace', exact: true }).getAttribute('aria-expanded'), 'false');
  await opened(page);
  await page.evaluate(() => window.overlayFixture.setExpanded(true));
  await content.waitFor({ state: 'detached' });
});

test('workspace shortcut toggles the menu and leaves a nested dialog in control', async t => {
  const page = await pageFor(t);
  await page.keyboard.press('Control+.');
  const content = page.getByRole('dialog', { name: 'Workspace resources', exact: true });
  await content.waitFor();
  await content.getByRole('button', { name: 'Add pull requests', exact: true }).click();
  const nested = page.getByRole('dialog', { name: 'Attach pull request', exact: true });
  await nested.waitFor();
  await page.keyboard.press('Control+.');
  assert.equal(await nested.isVisible(), true);
  await page.keyboard.press('Escape');
  await nested.waitFor({ state: 'detached' });
  await page.keyboard.press('Control+.');
  await content.waitFor({ state: 'detached' });
});

test('reduced motion removes both animations without losing keyboard dismissal', async t => {
  const page = await pageFor(t, { reducedMotion: 'reduce' });
  const content = await opened(page);
  assert.equal(await content.evaluate(element => getComputedStyle(element).animationName), 'none');
  await page.keyboard.press('Escape');
  await content.waitFor({ state: 'detached' });
  await page.waitForFunction(() => document.activeElement?.textContent === 'Workspace');
  assert(await page.getByRole('button', { name: 'Workspace', exact: true }).evaluate(element => element === document.activeElement));
});
