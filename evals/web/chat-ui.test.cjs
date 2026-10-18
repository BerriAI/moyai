const assert = require("node:assert/strict")
const { test } = require("node:test")
const { createServer } = require("node:http")
const { readFile } = require("node:fs/promises")
const path = require("node:path")
const { chromium } = require("playwright")

async function openApp(t, handleApi) {
  const root = path.join(__dirname, "dist")
  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname
    const target = path.resolve(root, `.${pathname === "/" ? "/index.html" : pathname}`)
    if (!target.startsWith(`${root}${path.sep}`)) { response.writeHead(404).end(); return }
    try {
      const content = await readFile(target)
      const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".ttf": "font/ttf" }[path.extname(target)]
      response.writeHead(200, { "Content-Type": type || "application/octet-stream" }).end(content)
    } catch { response.writeHead(404).end("Build evals/web before running this test.") }
  })
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  const browser = await chromium.launch({ headless: true })
  t.after(async () => { await browser.close(); await new Promise((resolve) => server.close(resolve)) })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.setDefaultTimeout(5000)
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  await page.route("**/api/**", handleApi)
  const url = `http://127.0.0.1:${server.address().port}`
  return { page, url, errors }
}

const ready = { ready: true, phase: "ready", model: "test/agent-model", context: "DeepSeek test workspace" }
const clone = (value) => JSON.parse(JSON.stringify(value))

test("local chat supports real API states, history, questions, and narrow screens", { timeout: 30000 }, async (t) => {
  const chats = []
  let submissions = 0
  const { page, url, errors } = await openApp(t, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (request.method() === "POST") assert.equal(request.headers()["x-moyai-local"], "1")
    if (pathname === "/api/status") return route.fulfill({ json: ready })
    if (pathname === "/api/chats" && request.method() === "GET") return route.fulfill({ json: { chats: clone(chats) } })
    if (pathname === "/api/chats") {
      const chat = { id: `chat-${chats.length + 1}`, title: "New chat", status: "idle", messages: [] }
      chats.unshift(chat)
      return route.fulfill({ json: clone(chat) })
    }
    const chat = chats.find((item) => item.id === pathname.split("/")[3])
    if (!chat) return route.fulfill({ status: 404, json: { detail: "Chat not found" } })
    if (request.method() === "GET") return route.fulfill({ json: clone(chat) })
    submissions += 1
    const payload = request.postDataJSON()
    assert.ok(payload.request_id)
    if (payload.answers) {
      assert.deepEqual(payload.answers, { Product: "LiteLLM" })
      chat.status = "idle"
      chat.messages.push({ id: "answer", role: "assistant", text: "Thanks. Checking LiteLLM.", status: "complete" })
    } else {
      chat.title = payload.message
      chat.status = "running"
      chat.messages.push({ id: "user", role: "user", text: payload.message }, { id: "assistant", role: "assistant", text: "", status: "running" })
    }
    return route.fulfill({ status: 202, json: clone(chat) })
  })
  await page.goto(url)
  await page.waitForFunction(() => !document.querySelector("button[aria-label='Send message']").disabled)
  assert.match(await page.getByLabel("Message Moyai").inputValue(), /DeepSeek-V4.1-Flash-1017/)
  for (const width of [1440, 768, 320]) {
    await page.setViewportSize({ width, height: 900 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `No page overflow at ${width}px`)
  }
  await page.emulateMedia({ reducedMotion: "reduce" })
  await page.getByRole("button", { name: "Open conversations" }).click()
  await page.getByRole("dialog").waitFor()
  await page.keyboard.press("Escape")
  await page.getByRole("dialog").waitFor({ state: "hidden" })
  await page.getByLabel("Message Moyai").press("Shift+Enter")
  assert.equal(submissions, 0, "Shift+Enter does not submit")
  await page.getByLabel("Message Moyai").press("Enter")
  await page.getByText("Reply and tool details appear when this turn finishes.").waitFor()
  assert.equal(submissions, 1)
  assert.equal(await page.getByRole("button", { name: "Send message", exact: true }).isDisabled(), true)

  const chat = chats[0]
  chat.status = "waiting"
  chat.messages[1] = {
    id: "assistant", role: "assistant", text: "Which **product** should I check? See the [model registry](litellm/model_prices_and_context_window.json), [local notes](file:///workspace/notes.md), and [provider documentation](https://example.com/models).", status: "waiting",
    tools: [{ name: "inspect_workspace", args: { path: "/workspace" }, result: "Found the LiteLLM repository." }],
    questions: [{ header: "Product", question: "Which product?", options: [{ label: "LiteLLM", description: "Check the repository" }, { label: "Moyai" }] }],
  }
  await page.getByText("Moyai needs your input").waitFor()
  assert.equal(await page.getByRole("link", { name: "model registry" }).count(), 0)
  assert.equal(await page.getByRole("link", { name: "local notes" }).count(), 0)
  assert.equal(await page.getByText("model registry", { exact: true }).getAttribute("title"), "litellm/model_prices_and_context_window.json")
  assert.equal(await page.getByRole("link", { name: "provider documentation" }).getAttribute("href"), "https://example.com/models")
  await page.getByText("1 tool call", { exact: true }).click()
  await page.getByText("inspect_workspace", { exact: true }).click()
  await page.getByText("Found the LiteLLM repository.", { exact: true }).waitFor()
  await page.getByRole("button", { name: "LiteLLM Check the repository" }).click()
  await page.getByRole("button", { name: "Continue conversation" }).click()
  await page.getByText("Thanks. Checking LiteLLM.").waitFor()
  assert.equal(submissions, 2)
  assert.equal(await page.getByRole("button", { name: "Continue conversation" }).isDisabled(), true, "Answered question is disabled")
  chat.status = "waiting"
  chat.messages.push({ id: "second-question", role: "assistant", text: "One more question.", status: "waiting", questions: [{ header: "Scope", question: "Which scope?", options: [{ label: "Current main" }] }] })
  await page.getByText("One more question.").waitFor()
  const answerInputs = page.getByLabel("Your answer", { exact: true })
  assert.equal(await answerInputs.count(), 2)
  assert.equal(await answerInputs.nth(0).isDisabled(), true)
  assert.equal(await answerInputs.nth(1).isEnabled(), true)
  const ids = await answerInputs.evaluateAll((inputs) => inputs.map((input) => input.id))
  assert.equal(new Set(ids).size, 2, "Each question form has unique input labels")
  chat.status = "idle"
  chat.messages.at(-1).status = "complete"
  await page.reload()
  await page.getByText("Thanks. Checking LiteLLM.").waitFor()
  await page.getByRole("button", { name: "Open conversations" }).click()
  await page.getByRole("button", { name: "New chat", exact: true }).click()
  await page.getByText("Try a conversation.").waitFor()
  await page.getByLabel("Message Moyai").fill("Keep this draft")
  await page.getByRole("button", { name: "Open conversations" }).click()
  await page.getByRole("button", { name: /do we support/ }).click()
  await page.getByText("Thanks. Checking LiteLLM.").waitFor()
  await page.getByRole("button", { name: "Open conversations" }).click()
  await page.getByRole("dialog").locator(".local-chat-link").filter({ hasText: "New chat" }).click()
  await page.waitForFunction(() => document.querySelector("#chat-message").value === "Keep this draft")
  assert.deepEqual(errors, [])
})

test("unknown send outcomes reuse the request ID, and old chat links recover", { timeout: 20000 }, async (t) => {
  const chat = { id: "chat-retry", title: "Retry test", status: "idle", messages: [] }
  const ids = []
  let deleted = false
  const { page, url, errors } = await openApp(t, async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === "/api/status") return route.fulfill({ json: ready })
    if (pathname === "/api/chats" && request.method() === "GET") return route.fulfill({ json: { chats: deleted ? [] : [clone(chat)] } })
    if (pathname === "/api/chats") return route.fulfill({ json: clone(chat) })
    if (deleted) return route.fulfill({ status: 404, json: { detail: "Chat not found" } })
    if (request.method() === "GET") return route.fulfill({ json: clone(chat) })
    ids.push(request.postDataJSON().request_id)
    if (ids.length === 1) return route.abort("failed")
    chat.messages = [{ id: "done", role: "assistant", text: "Accepted once.", status: "complete" }]
    return route.fulfill({ status: 202, json: clone(chat) })
  })
  await page.goto(`${url}/#chat=chat-retry`)
  await page.waitForFunction(() => !document.querySelector("#chat-message").disabled)
  await page.getByLabel("Message Moyai").fill("test")
  await page.getByRole("button", { name: "Send message", exact: true }).click()
  await page.getByText(/Your draft has been kept/).waitFor()
  await page.getByRole("button", { name: "Send message", exact: true }).click()
  await page.getByText("Accepted once.").waitFor()
  assert.equal(ids.length, 2)
  assert.equal(ids[0], ids[1])
  deleted = true
  await page.reload()
  await page.getByText(/This conversation is no longer available/).waitFor()
  await page.getByText("Try a conversation.").waitFor()
  assert.equal(new URL(page.url()).hash, "")
  assert.deepEqual(errors, [])
})
