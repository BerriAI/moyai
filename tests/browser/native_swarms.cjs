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
import { setCsrf } from '../native/web/api';
import '../frontend/theme.css';
import '../native/web/style.css';
setCsrf('synthetic-csrf');
function Fixture() {
  const [id, setId] = useState('mission-one');
  const [options, setOptions] = useState({ workers:20, budget_seconds:0, worker_models:[] });
  window.swarmFixture = { setId };
  return <main>
    <SwarmSetup value={options} onChange={setOptions} models={['openai/test','anthropic/test']} disabled={false} />
    <output aria-label="Selected workers">{options.workers}</output>
    <SwarmPanel sessionId={id} onChanged={async () => {}} />
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
    server: { host: "127.0.0.1", port: 0, strictPort: true },
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
  test(`swarm roster and controls remain usable at ${width}px`, async t => {
    const page = await pageFor(t, width);
    const panel = page.getByRole("region", { name: "Swarm mission", exact: true });
    await panel.waitFor();
    assert.equal(await panel.locator(".swarm-member").count(), 21);
    assert.equal(await panel.getByRole("button", { name: "Pause team", exact: true }).count(), 1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await panel.locator(".swarm-member").first().getAttribute("href"), "#mission-one");
    await page.getByRole("combobox", { name: "Swarm workers", exact: true }).click();
    await page.getByRole("option", { name: "2 workers", exact: true }).click();
    assert.equal(await page.getByLabel("Selected workers").textContent(), "2");
  });
}

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
  await page.getByRole("combobox", { name: "Change swarm work window", exact: true }).click();
  await page.getByRole("option", { name: "30 minutes", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="Change swarm work window"]')?.textContent.includes("30 minutes"));
  assert.deepEqual(writes.map(body => body.action), ["pause", "resume", "time"]);
  assert.equal(writes[2].budget_seconds, 1800);
  assert.equal(await page.locator(".swarm-member").first().getAttribute("href"), "#mission-one");
});

test("shared viewers can inspect the team but receive no mutation controls", async t => {
  const page = await pageFor(t, 768, route => route.fulfill({ json: { ...mission(), can_control: false } }));
  await page.getByRole("region", { name: "Swarm mission" }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Pause team", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Stop team", exact: true }).count(), 0);
  assert.equal(await page.getByRole("combobox", { name: "Change swarm work window", exact: true }).count(), 0);
  assert.equal(await page.locator('.swarm-member[href="#worker-1"]').count(), 1);
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
  await page.getByText("Second mission coordinator", { exact: true }).waitFor();
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.getByText("Second mission coordinator", { exact: true }).count(), 1);
  assert.equal(await page.locator('.swarm-member[href="#mission-one"]').count(), 0);
});
