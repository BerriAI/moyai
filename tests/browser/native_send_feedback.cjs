// Actual React app with controlled API delays. Live app/model evidence is separate.
const { before, after, test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright");
let server, browser, base;
const gate = () => {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
};
before(async () => {
  const { createServer } = await import("vite");
  server = await createServer({
    configFile: path.resolve("native/web/vite.config.ts"),
    server: { host: "127.0.0.1", port: 0, hmr: false }, logLevel: "error",
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch();
});
after(async () => { await browser?.close(); await server?.close(); });

async function scenario(t, options = {}) {
  const page = await browser.newPage({
    viewport: { width: options.width || 1440, height: 900 }, reducedMotion: "reduce",
  });
  const state = {
    posts: [], turns: options.turns ?? [], messages: options.messages ?? [],
    reads: 0, historyReads: 0, stops: 0, streams: [], signedOut: false,
    session: { id: "chat", title: "Send feedback", status: "idle", active_turn: null,
      model: "test-model", visibility: "private", surface: "web", owner_id: "viewer" },
  };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  if (options.light) await page.addInitScript(() => localStorage.setItem("moyai-appearance", "light"));
  await page.route(/^https?:\/\/[^/]+\/api\//, async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, json: value });
    if (pathname === "/api/me") return state.signedOut
      ? json({ detail: "Sign in again." }, 401)
      : json({ user: { id: "viewer", name: "Test viewer", role: "admin", individual: true },
        csrf: "test-csrf", models: ["test-model"], default_model: "test-model",
        product_mode: "standalone", preferences: { connection_ids: [], delivery_mode: "when_idle" } });
    if (pathname === "/api/auth/config") return json({ mode: "standalone", password_enabled: true });
    if (pathname === "/api/history/sessions") {
      if (state.posts.length && options.sidebar) await options.sidebar.promise;
      if (state.posts.length && (typeof options.sidebarFailure === "function" ? options.sidebarFailure() : options.sidebarFailure)) {
        return json({ detail: "Sidebar unavailable." }, 503);
      }
      return json({ sessions: [], ancestors: [], has_more: false, next_cursor: null });
    }
    if (pathname === "/api/sessions/chat/messages" && request.method() === "POST") {
      const body = request.postDataJSON();
      state.posts.push(body);
      if (options.post) await options.post.promise;
      if (options.reject?.(state)) return json({ detail: "Admission unavailable. Retry." }, 503);
      const turn = {
        id: `turn-${state.posts.length}`, actor_id: "viewer", session_id: "chat", status: "queued",
        model: body.model, mode: body.mode, connections: body.connections, created_at: new Date().toISOString(),
        accepted_at: null, enqueue_id: null, parent_turn: null, error: null, finished_at: null, usage: {},
      };
      state.turns.unshift(turn);
      state.messages.push({ id: Math.max(options.messageFloor ?? 0, ...state.messages.map(row => row.id)) + 1, turn_id: turn.id, author_id: "viewer",
        role: "user", content: body.prompt, attachments: [] });
      return json(turn, 202);
    }
    if (pathname === "/api/sessions/chat") {
      state.reads++;
      if (state.posts.length && options.read) await options.read.promise;
      if (state.reads > 2 && options.refreshDelay) await new Promise(resolve => setTimeout(resolve, options.refreshDelay));
      if (options.denied?.(state)) return json({ detail: "Conversation access revoked." }, 403);
      if (state.posts.length && options.running && state.turns[0].status === "queued") {
        state.turns[0].status = "running";
        state.session.active_turn = state.turns[0].id;
        state.session.status = "running";
      }
      return json({ session: state.session, turns: state.turns, messages: options.history?.() ?? state.messages,
        transcript_version: String(state.messages.length), pending: null });
    }
    if (pathname === "/api/sessions/chat/messages") {
      state.historyReads++;
      const snapshot = [...state.messages];
      if (options.historyPage) await options.historyPage.promise;
      if (options.denyHistory?.()) return json({ detail: "Conversation access revoked." }, 403);
      if (options.historyFailure?.()) return json({ detail: "History page unavailable." }, 503);
      return json(options.snapshotPages ? snapshot : state.messages);
    }
    if (pathname === "/api/sessions/chat/stop") {
      state.stops++;
      state.turns[0].status = "stopped";
      state.session.active_turn = null; state.session.status = "idle";
      return json({ ok: true });
    }
    if (pathname === "/api/sessions/chat/events") {
      state.streams.push(url);
      const body = options.events?.(state) ?? "event: connected\ndata: {}\n\n";
      return route.fulfill({ contentType: "text/event-stream", body });
    }
    if (pathname.endsWith("/work") || pathname.includes("/progress/")) return json({ messages: [], tools: [], subagents: [] });
    if (pathname.endsWith("/workspace")) return json({ links: [], files: [], sources: [], subagents: [] });
    if (pathname === "/api/skills") return json({ skills: [], has_more: false });
    if (pathname.endsWith("/read")) return json({ ok: true });
    return json([]);
  });
  await page.goto(`${base}/#chat`);
  const input = page.getByRole("textbox", { name: "Message Moyai", exact: true });
  await input.waitFor({ timeout: 10000 }).catch(async error => {
    throw new Error(`${error.message}\n${await page.locator("body").innerText()}\n${errors.join("\n")}`);
  });
  return { page, input, state };
}

for (const width of [1440, 768, 320]) test(`admission feedback and concurrent sends bypass refreshes at ${width}px`, async t => {
  const post = gate(), read = gate(), sidebar = gate();
  t.after(() => { post.release(); read.release(); sidebar.release(); });
  const { page, input, state } = await scenario(t, { width, light: width === 768, post, read, sidebar });
  await input.fill("First accepted input");
  // Two events in one browser task cannot race React's busy state update.
  await page.locator(".composer-wrap").evaluate(form => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await page.getByRole("status").filter({ hasText: /^Sending…$/ }).waitFor();
  assert.equal(state.posts.length, 1);
  assert.equal(await page.locator(".message.user").count(), 0, "No accepted bubble before the receipt");
  post.release();
  await page.locator(".message.user").filter({ hasText: "First accepted input" }).waitFor();
  await input.fill("Second accepted input");
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Second accepted input" }).waitFor();
  assert.equal(state.posts.length, 2);
  assert.notEqual(state.posts[0].request_key, state.posts[1].request_key);
  assert.equal(state.streams.length, 0, "Queued receipts must not open premature streams");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  read.release(); sidebar.release();
  await page.waitForResponse(response => response.url().includes("/read"));
  assert.equal(await page.locator(".message.user").count(), 2, "Saved messages replace receipt bubbles");
  await input.fill("Draft survives background refresh");
  assert.equal(await input.innerText(), "Draft survives background refresh");
});

test("failed admission keeps the draft and request key; accepted refresh failure cannot invite resubmission", async t => {
  let reject = true;
  const read = gate();
  t.after(() => read.release());
  const { page, input, state } = await scenario(t, { reject: () => reject, read });
  await input.fill("Retry this once");
  await input.press("Enter");
  await page.getByRole("alert").filter({ hasText: "Admission unavailable" }).waitFor();
  assert.equal(await input.innerText(), "Retry this once");
  assert.equal(await page.locator(".message.user").count(), 0);
  reject = false;
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Retry this once" }).waitFor();
  assert.equal(state.posts.length, 2);
  assert.equal(state.posts[0].request_key, state.posts[1].request_key);
  assert.equal((await input.innerText()).trim(), "");
  await page.route("**/api/sessions/chat?**", route => route.fulfill({ status: 503, json: { detail: "History unavailable" } }));
  read.release();
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.getByRole("alert").filter({ hasText: "History unavailable" }).waitFor();
  assert.equal(await page.locator(".message.user").filter({ hasText: "Retry this once" }).count(), 1);
  assert.equal(state.posts.length, 2);
});

test("running status, reconnect and Stop remain independent of history pagination", async t => {
  const historyPage = gate();
  t.after(() => historyPage.release());
  let history = [];
  const { page, input, state } = await scenario(t, {
    running: true, historyPage, history: () => history,
    events: state => state.streams.length === 1
      ? 'event: connected\ndata: {}\n\nid: 0\ndata: {"type":"response_start","run_id":"turn-1","step":0}\n\nid: 1\ndata: {"type":"text_start","index":0,"text":"Live ","phase":"final_answer"}\n\nevent: finished\ndata: {}\n\n'
      : 'event: connected\ndata: {}\n\nid: 2\ndata: {"type":"text_delta","index":0,"text":"response before history","phase":"final_answer"}\n\n',
  });
  history = Array.from({ length: 200 }, (_, index) => ({
    id: index + 1, turn_id: "old", author_id: "viewer", role: "user", content: `Earlier ${index}`,
  }));
  await input.fill("Start live work");
  await input.press("Enter");
  await page.locator(".live-message").filter({ hasText: "Live response before history" }).waitFor();
  assert.ok(state.streams.length >= 2);
  assert.equal(state.streams[0].searchParams.get("turn_id"), "turn-1");
  assert.equal(state.streams[1].searchParams.get("offset"), "2", "Reconnect resumes the existing cursor");
  await page.getByText("Earlier 199", { exact: true }).waitFor();
  await page.getByText("Loading conversation history…", { exact: true }).waitFor();
  assert.equal(state.historyReads, 1, "Reconnect refreshes share the transcript revision's paging request");
  assert.equal(await input.getAttribute("contenteditable"), "true");
  await page.getByRole("button", { name: "Stop task", exact: true }).click();
  await page.locator(".topbar .status").filter({ hasText: "idle" }).waitFor();
  assert.equal(state.stops, 1);
  assert.equal(state.historyReads, 1, "Action metadata refresh does not discard known history or restart identical paging");
  // Complete pagination with monotonically increasing IDs.
  state.messages[0].id = 201;
  historyPage.release();
  await page.getByText("Loading conversation history…", { exact: true }).waitFor({ state: "hidden" });
  assert.equal(await page.locator(".message.user").filter({ hasText: "Start live work" }).count(), 1);
});

const historyRows = (start, count, turn = "old") => Array.from({ length: count }, (_, index) => ({
  id: start + index, turn_id: turn, author_id: "viewer", role: "user", content: `Earlier ${start + index}`,
}));

test("opening 220 messages and sending during pagination retains known pages and reconciles the receipt", async t => {
  const read = gate(), historyPage = gate();
  t.after(() => { read.release(); historyPage.release(); });
  const { page, input, state } = await scenario(t, {
    read, historyPage, snapshotPages: true,
    history: () => historyRows(1, 200), messages: historyRows(201, 20, "tail-turn"),
    turns: [{ id: "tail-turn", actor_id: "viewer", model: "test-model", mode: "when_idle",
      status: "completed", created_at: "2026-10-19T00:00:00Z", finished_at: "2026-10-19T00:00:01Z" }],
  });
  await page.getByText("Earlier 200", { exact: true }).waitFor();
  await page.getByText("Loading conversation history…", { exact: true }).waitFor();
  assert.equal(await page.locator(".empty-chat").count(), 0);
  assert.equal(await page.locator('.turn-progress[data-turn-id="tail-turn"]').count(), 0,
    "A missing historical input is not yet an unanchored continuation");
  await input.fill("Accepted while history is incomplete");
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Accepted while history is incomplete" }).waitFor();
  assert.equal(await page.locator(".message.user").count(), 201);
  await page.getByText("Earlier 200", { exact: true }).waitFor();
  // The older request completes without the new saved input: retain its receipt.
  historyPage.release();
  await page.getByText("Earlier 220", { exact: true }).waitFor();
  assert.equal(await page.locator(".message.user").count(), 221);
  assert.equal(state.posts.length, 1);
  read.release();
  await page.waitForResponse(response => response.url().includes("/messages?after="));
  await page.getByText("Loading conversation history…", { exact: true }).waitFor({ state: "hidden" });
  assert.equal(await page.locator(".message.user").count(), 221);
  assert.equal(await page.locator(".message.user").filter({ hasText: "Accepted while history is incomplete" }).count(), 1);
  assert.equal(await page.locator(".empty-chat").count(), 0);
});

test("revoked paginated history clears known pages and an accepted receipt", async t => {
  const historyPage = gate();
  let denied = false;
  t.after(() => historyPage.release());
  const { page, input } = await scenario(t, {
    historyPage, history: () => historyRows(1, 200), messages: historyRows(201, 20),
    denyHistory: () => denied,
  });
  await page.getByText("Earlier 200", { exact: true }).waitFor();
  await input.fill("Private accepted during paging");
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Private accepted during paging" }).waitFor();
  denied = true;
  historyPage.release();
  await page.getByRole("alert").filter({ hasText: "Conversation access revoked" }).waitFor();
  assert.equal(await page.locator(".message").count(), 0);
  assert.equal(await page.locator(".composer-wrap").count(), 0);
});

test("a failed history page retains known rows and retries without submitting the draft", async t => {
  const historyPage = gate();
  let failing = true;
  t.after(() => historyPage.release());
  const { page, input, state } = await scenario(t, {
    historyPage, history: () => historyRows(1, 200), messages: historyRows(201, 20),
    historyFailure: () => failing,
  });
  await page.getByText("Earlier 200", { exact: true }).waitFor();
  await input.fill("Keep this unsent draft");
  historyPage.release();
  await page.getByRole("alert").filter({ hasText: "History page unavailable" }).waitFor();
  await page.getByText("Conversation history is incomplete.", { exact: true }).waitFor();
  assert.equal(await page.locator(".message.user").count(), 200);
  assert.equal(await page.locator(".empty-chat").count(), 0);
  failing = false;
  await page.getByRole("button", { name: "Retry history", exact: true }).click();
  await page.getByText("Earlier 220", { exact: true }).waitFor();
  assert.equal(await input.innerText(), "Keep this unsent draft");
  assert.equal(state.posts.length, 0);
  assert.equal(await page.getByRole("alert").count(), 0);
});

test("successful conversation history cannot clear a failed sidebar refresh notice", async t => {
  const read = gate();
  let sidebarFailing = true;
  t.after(() => read.release());
  const { page, input, state } = await scenario(t, { read, sidebarFailure: () => sidebarFailing });
  await input.fill("Send despite a failed sidebar refresh");
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Send despite a failed sidebar refresh" }).waitFor();
  const notice = page.getByRole("alert").filter({ hasText: "Sidebar unavailable." });
  await notice.waitFor();
  const loaded = page.waitForResponse(response => response.url().includes("/read"));
  read.release(); await loaded;
  assert.equal(await notice.isVisible(), true);
  assert.equal((await input.innerText()).trim(), "");
  assert.equal(state.posts.length, 1, "A refresh failure never replays an accepted send");
  sidebarFailing = false;
  const refreshed = page.waitForResponse(response => response.url().includes("/api/history/sessions"));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await refreshed;
  await notice.waitFor({ state: "hidden" });
  assert.equal(state.posts.length, 1, "Only a successful inventory refresh clears its notice");
});

test("a late accepted receipt cannot restore content after sign-out", async t => {
  const post = gate();
  t.after(() => post.release());
  const { page, input, state } = await scenario(t, { post });
  await input.fill("Private pending content");
  await input.press("Enter");
  await page.getByRole("status").filter({ hasText: /^Sending…$/ }).waitFor();
  state.signedOut = true;
  await page.evaluate(() => window.dispatchEvent(new Event("moyai:signed-out")));
  await page.getByRole("heading", { name: "Welcome to Moyai" }).waitFor();
  const response = page.waitForResponse(response => response.request().method() === "POST");
  post.release(); await response;
  assert.equal(await page.getByText("Private pending content", { exact: true }).count(), 0);
  assert.equal(state.streams.length, 0);
});

test("repeated early stream endings cannot starve a slow terminal-state refresh", async t => {
  const { page, input, state } = await scenario(t, {
    running: true, refreshDelay: 1800,
    events: state => {
      state.turns[0].status = "completed";
      state.session.status = "idle";
      state.session.active_turn = null;
      return "event: connected\ndata: {}\n\nevent: finished\ndata: {}\n\n";
    },
  });
  await input.fill("Complete while reads are slow");
  await input.press("Enter");
  await page.locator(".topbar .status").filter({ hasText: "running" }).waitFor();
  await page.getByText("Reconnecting to live progress…", { exact: true }).waitFor();
  // A visibility poll competes with the stream's outstanding refresh.
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.locator(".topbar .status").filter({ hasText: "idle" }).waitFor({ timeout: 7000 });
  assert.ok(state.streams.length >= 2, "The read outlives the reconnect interval");
  assert.ok(state.streams.length <= 3, "Terminal state ends retries promptly");
});

test("denied history clears a previously accepted message", async t => {
  const read = gate();
  t.after(() => read.release());
  const { page, input } = await scenario(t, { read, denied: state => state.posts.length > 0 });
  await input.fill("Private accepted content");
  await input.press("Enter");
  await page.locator(".message.user").filter({ hasText: "Private accepted content" }).waitFor();
  read.release();
  await page.getByRole("alert").filter({ hasText: "Conversation access revoked" }).waitFor();
  assert.equal(await page.getByText("Private accepted content", { exact: true }).count(), 0);
  assert.equal(await page.locator(".composer-wrap").count(), 0);
});
