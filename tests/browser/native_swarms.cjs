// Actual React/Radix swarm components with synthetic API responses.
// Supplements the separate real-app/model/Modal before/after proof.
const { before, after, test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");
const os = require("node:os");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "../..");
const entry = path.join(root, "tests/__swarm_fixture.tsx");
const moduleUrl = "/tests/__swarm_fixture.tsx";
let server, browser, address, cache;
const fixture = `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { SwarmPanel, SwarmSetup } from '../native/web/swarm';
import { Home } from '../native/web/home';
import { setCsrf } from '../native/web/api';
import '../frontend/theme.css';
import '../native/web/style.css';
setCsrf('synthetic-csrf');
function Fixture() {
  const [id, setId] = useState('mission-one');
  const [options, setOptions] = useState({ workers:20, budget_seconds:0, worker_models:[] });
  const [space, setSpace] = useState(false);
  window.swarmFixture = { setId, setSpace };
  if (location.search === '?home') return <Home models={['test-model']} defaultModel="test-model"
    onCreated={async () => {}} onStarted={value => { window.startedConversation = value; }} />;
  return <main>
    <SwarmSetup value={options} onChange={setOptions} models={['openai/test','anthropic/test']} disabled={false} />
    <output aria-label="Selected workers">{options.workers}</output>
    <div className={'chat' + (space ? ' swarm-space-mode' : '')} style={{height:700}}>
      <SwarmPanel sessionId={id} onChanged={async () => {}} space={space} onSpaceChange={setSpace} />
    </div>
  </main>;
}
createRoot(document.getElementById('root')).render(<Fixture />);
`;
const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>Swarm component regression</title>
<div id="root"></div><script type="module" src="${moduleUrl}"></script></html>`;

function mission(id = "mission-one") {
  return {
    id, status: "active", reason: "", revision: 0, generation: 1, round: 1,
    goal: "Investigate one shared goal.", budget_seconds: 0, ends_at: null,
    leader_id: "worker-1", can_control: true, sequence: 1, has_more: false, next_after: 1,
    members: Array.from({ length: 21 }, (_, index) => ({
      session_id: index ? `worker-${index}` : id, name: index ? `Agent ${index}` : "Coordinator",
      model: "openai/a-long-but-valid-model-name-for-responsive-testing", coordinator: index === 0,
      status: index < 3 ? "running" : "queued", active_turn: index < 3 ? `turn-${index}` : null,
      assignment: "", deleted: false,
    })),
    posts: [{
      id: "post-one", sequence: 1, kind: "finding", author: "Agent 1", author_session_id: "worker-1",
      content: "Verified result **5050** from the member computer.", created_at: "2026-10-16T19:00:00Z",
    }],
  };
}

before(async () => {
  const { createServer } = await import("vite");
  const { default: tailwindcss } = await import("@tailwindcss/vite");
  cache = await fs.mkdtemp(path.join(os.tmpdir(), "moyai-swarm-vite-"));
  server = await createServer({
    configFile: false, root, cacheDir: cache, logLevel: "error",
    resolve: { alias: { "@": path.join(root, "frontend") } },
    // Lazy decoration must not trigger a Vite reload while the form is in use.
    optimizeDeps: {
      entries: ["native/web/main.tsx"],
      include: ["three", "three/examples/jsm/loaders/GLTFLoader.js"],
    },
    server: { host: "127.0.0.1", port: 0, strictPort: false },
    plugins: [tailwindcss(), {
      name: "swarm-component-regression",
      resolveId(id) { if (id === moduleUrl) return entry; },
      load(id) { if (id === entry) return fixture; },
      configureServer(vite) {
        vite.middlewares.use((request, response, next) => {
          if (new URL(request.url, "http://localhost").pathname !== "/") return next();
          void vite.transformIndexHtml("/", html).then(value => {
            response.setHeader("Content-Type", "text/html"); response.end(value);
          }).catch(next);
        });
      },
    }],
  });
  await server.listen();
  address = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch();
  await server.transformRequest(moduleUrl);
});
after(async () => {
  await browser?.close(); await server?.close();
  if (cache) await fs.rm(cache, { recursive: true, force: true });
});

async function pageFor(t, width = 1440, handler) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const page = await context.newPage();
  await page.route("**/api/**", handler || (route => route.fulfill({ json: mission() })));
  t.after(async () => { await page.unrouteAll({ behavior: "wait" }); await context.close(); });
  await page.goto(address);
  return page;
}

for (const width of [1440, 768, 320]) {
  test(`swarm Chat stays compact with usable controls at ${width}px`, async t => {
    const page = await pageFor(t, width);
    const panel = page.getByRole("region", { name: "Swarm mission", exact: true });
    await panel.waitFor();
    assert.equal(await panel.locator(".swarm-member").count(), 0);
    assert.equal(await panel.getByRole("button", { name: "Pause team", exact: true }).count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await panel.locator(".swarm-details").count(), 0);
    await page.getByRole("spinbutton", { name: "Swarm workers", exact: true }).fill("3");
    assert.equal(await page.getByLabel("Selected workers").textContent(), "3");
  });
}

test("custom worker counts accept whole numbers and preserve invalid drafts for correction", async t => {
  const page = await pageFor(t);
  const input = page.getByRole("spinbutton", { name: "Swarm workers", exact: true });
  assert.equal(await input.getAttribute("max"), null);
  for (const value of ["2", "3", "7", "17", "20", "21", "37", "1000"]) {
    await input.fill(value);
    assert.equal(await input.evaluate(element => element.checkValidity()), true);
    assert.equal(await page.getByLabel("Selected workers").textContent(), value);
  }
  for (const value of ["", "0", "1", "-1", "2.5"]) {
    await input.fill(value);
    assert.equal(await input.inputValue(), value);
    assert.equal(await input.evaluate(element => element.checkValidity()), false);
    assert.equal(await input.getAttribute("aria-invalid"), "true");
    await page.getByText("Enter a whole number of at least 2.", { exact: true }).waitFor();
  }
});

test("home starts a normal conversation for a request to create a swarm", async t => {
  const writes = [];
  const page = await pageFor(t, 1440, async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() === "POST") writes.push({ path, body: request.postDataJSON() });
    await route.fulfill({ json: path === "/api/sessions" ? { id: "created-chat" } : { id: "initial-turn" } });
  });
  await page.goto(address + "?home");
  assert.equal(await page.getByRole("button", { name: "Swarm", exact: true }).count(), 0);
  assert.equal(await page.getByRole("spinbutton", { name: "Swarm workers", exact: true }).count(), 0);
  const prompt = page.getByRole("textbox", { name: "Message Moyai", exact: true });
  await prompt.fill("Create a swarm with 21 workers to investigate this bug.");
  await prompt.press("Enter");
  await page.waitForFunction(() => Boolean(window.startedConversation));
  assert.deepEqual(writes.map(row => row.path), ["/api/sessions", "/api/sessions/created-chat/messages"]);
  assert.equal(writes[0].body.visibility, "shared");
  assert.equal(writes[1].body.prompt, "Create a swarm with 21 workers to investigate this bug.");
  assert.equal(writes[1].body.workers, undefined);
});

for (const width of [1440, 768, 320]) {
  test(`live space shows every member without overlap or horizontal overflow at ${width}px`, async t => {
    const page = await pageFor(t, width);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByRole("button", { name: "Space", exact: true }).click();
    const space = page.getByRole("region", { name: "Live swarm space", exact: true });
    await space.waitFor();
    assert.equal(await space.locator(".orbit-sun").getAttribute("data-agent-id"), "worker-1");
    const stage = await space.locator(".orbit-stage").boundingBox(), sun = await space.locator(".orbit-sun").boundingBox();
    assert(Math.abs(sun.x + sun.width / 2 - stage.x - stage.width / 2) < 2, "Lead is centered on the orbits");
    assert(Math.abs(sun.y + sun.height / 2 - stage.y - stage.height / 2) < 2, "Lead is vertically centered");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const reachable = new Set();
    // Small screens page real agents; every saved member must remain reachable.
    for (let pageIndex = 0; pageIndex < 25; pageIndex++) {
      const boxes = await space.locator(".orbit-agent").evaluateAll(nodes => nodes.map(node => {
        const { x, y, width, height } = node.getBoundingClientRect();
        return { id: node.dataset.agentId, x, y, width, height };
      }));
      boxes.forEach(box => reachable.add(box.id));
      for (let index = 0; index < boxes.length; index++) {
        for (let other = index + 1; other < boxes.length; other++) {
          const a = boxes[index], b = boxes[other];
          assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y,
            `members ${a.id} and ${b.id} overlap at ${width}px`);
        }
      }
      const next = space.getByRole("button", { name: "Next agents", exact: true });
      if (!(await next.count()) || !(await next.isEnabled())) break;
      await next.click();
    }
    assert.equal(reachable.size, 21);
    assert(reachable.has("worker-20"));
    await page.getByRole("button", { name: "Chat", exact: true }).click();
    assert.equal(await space.count(), 0);
    assert.equal(await page.locator(".swarm-member").count(), 0);
  });
}

test("space refreshes public replies and leadership, and coordinator links open chat", async t => {
  let state = mission();
  const page = await pageFor(t, 1440, route => route.fulfill({ json: state }));
  await page.getByRole("button", { name: "Space", exact: true }).click();
  await page.getByRole("button", { name: "Agent 1 · Working · View details", exact: true }).click();
  const preview = page.getByRole("dialog", { name: "Agent 1 details", exact: true });
  await preview.getByText(/Verified result/).waitFor();
  state = { ...state, leader_id: "worker-2", sequence: 2, posts: [...state.posts, {
    id: "post-two", sequence: 2, kind: "result", author: "Agent 1", author_session_id: "worker-1",
    content: 'New independently computed result: 5050. <img src=x onerror="window.exposed=true">',
    created_at: "2026-10-16T19:01:00Z",
  }] };
  state.members[1] = { ...state.members[1], status: "idle", active_turn: null };
  await preview.getByText(/New independently computed result/).waitFor();
  assert.equal(await preview.getByRole("link", { name: "Open thread", exact: true }).getAttribute("href"), "#worker-1");
  assert.equal(await preview.locator("img").count(), 0);
  assert.equal(await page.evaluate(() => window.exposed), undefined);
  await page.getByRole("button", { name: "Agent 1 · Ready · View details", exact: true }).waitFor();
  assert.equal(await page.locator(".orbit-sun").getAttribute("data-agent-id"), "worker-2");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Coordinator · Working · View details", exact: true }).click();
  await page.getByRole("dialog", { name: "Coordinator details", exact: true })
    .getByRole("link", { name: "Open thread", exact: true }).click();
  assert.equal(await page.getByRole("region", { name: "Live swarm space", exact: true }).count(), 0);
  assert.equal(await page.locator(".swarm-member").count(), 0);
});

test("space clears protected previews when access is revoked", async t => {
  let denied = false;
  const page = await pageFor(t, 1440, route => route.fulfill(denied
    ? { status: 403, json: { detail: "Access changed." } } : { json: mission() }));
  await page.getByRole("button", { name: "Space", exact: true }).click();
  await page.getByRole("button", { name: "Agent 1 · Working · View details", exact: true }).click();
  await page.getByRole("dialog", { name: "Agent 1 details", exact: true }).waitFor();
  denied = true;
  await page.getByRole("status").getByText(/Team details are unavailable/).waitFor();
  assert.equal(await page.getByRole("region", { name: "Live swarm space", exact: true }).count(), 0);
  assert.equal(await page.getByText(/Verified result/).count(), 0);
});

test("the coordinator's team update returns to its conversation", async t => {
  const state = mission();
  state.posts = [{ ...state.posts[0], kind: "result", author: "Coordinator",
    author_session_id: state.id, content: "Coordinator final synthesis." }];
  const page = await pageFor(t, 1440, route => route.fulfill({ json: state }));
  await page.getByRole("button", { name: "Space", exact: true }).click();
  await page.locator(".swarm-space-feed > summary").click();
  await page.getByRole("link", { name: "Coordinator · Open thread", exact: true }).click();
  assert.equal(await page.getByRole("region", { name: "Live swarm space", exact: true }).count(), 0);
  assert.equal(await page.locator(".swarm-member").count(), 0);
});

test("Space retains every recent public update and its full text", async t => {
  const state = mission();
  state.posts = Array.from({ length: 12 }, (_, index) => ({
    ...state.posts[0], id: `post-${index}`, sequence: index + 1,
    content: index === 0 ? "Oldest update " + "detail ".repeat(400) + "COMPLETE_END" : `Update ${index}`,
  }));
  const page = await pageFor(t, 1440, route => route.fulfill({ json: state }));
  assert.equal(await page.locator(".swarm-space-feed").count(), 0);
  await page.getByRole("button", { name: "Space", exact: true }).click();
  assert.equal(await page.locator(".swarm-space-feed article").count(), 0);
  await page.locator(".swarm-space-feed > summary").click();
  await page.getByText(/COMPLETE_END/).waitFor();
  assert.equal(await page.locator(".swarm-space-feed article").count(), 12);
});

test("owner controls use current revision and preserve the same mission", async t => {
  let state = mission();
  const writes = [];
  const page = await pageFor(t, 1440, async route => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON(); writes.push(body);
      assert.equal(body.revision, state.revision);
      assert.equal(route.request().headers()["x-moyai-csrf"], "synthetic-csrf");
      state = { ...state, revision: state.revision + 1,
        status: body.action === "pause" ? "paused" : body.action === "resume" ? "active" : state.status,
        budget_seconds: body.budget_seconds ?? state.budget_seconds };
    }
    await route.fulfill({ json: state });
  });
  await page.getByRole("button", { name: "Pause team", exact: true }).click();
  await page.getByRole("button", { name: "Resume team", exact: true }).click();
  await page.getByRole("button", { name: "Change work window", exact: true }).click();
  await page.getByRole("combobox", { name: "Change swarm work window", exact: true }).click();
  await page.getByRole("option", { name: "30 minutes", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="Change swarm work window"]')?.textContent.includes("30 minutes"));
  assert.deepEqual(writes.map(body => body.action), ["pause", "resume", "time"]);
  assert.equal(writes[2].budget_seconds, 1800);
  assert.equal(await page.locator(".swarm-roster").count(), 0);
});

test("shared viewers can inspect the team but receive no mutation controls", async t => {
  const page = await pageFor(t, 768, route => route.fulfill({ json: { ...mission(), can_control: false } }));
  await page.getByRole("region", { name: "Swarm mission" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Pause team", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Stop team", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Change work window", exact: true }).count(), 0);
  assert.equal(await page.getByRole("combobox", { name: "Change swarm work window", exact: true }).count(), 0);
  await page.getByRole("button", { name: "Space", exact: true }).click();
  await page.getByRole("button", { name: "Agent 1 · Working · View details", exact: true }).waitFor();
});

test("late data from a previous mission cannot replace the current roster", async t => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const page = await pageFor(t, 1440, async route => {
    if (route.request().url().includes("mission-one")) {
      await pending;
      await route.fulfill({ json: mission() }).catch(() => {});
    } else {
      const data = mission("mission-two");
      data.members[0].name = "Second mission coordinator";
      await route.fulfill({ json: data });
    }
  });
  await page.waitForFunction(() => Boolean(window.swarmFixture));
  await page.evaluate(() => window.swarmFixture.setId("mission-two"));
  await page.getByRole("button", { name: "Space", exact: true }).click();
  await page.getByRole("button", { name: "Second mission coordinator · Working · View details", exact: true }).waitFor();
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.getByRole("button", { name: "Second mission coordinator · Working · View details", exact: true }).count(), 1);
  assert.equal(await page.locator('.swarm-member[href="#mission-one"]').count(), 0);
});
