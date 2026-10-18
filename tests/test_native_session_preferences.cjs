const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const test = require("node:test")
const ts = require("typescript")
const exportsObject = {}
new Function("exports", ts.transpileModule(
  fs.readFileSync(path.join(__dirname, "../native/web/session-preferences.ts"), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText)(exportsObject)
const { SessionPreferenceUpdates, preferencePatch } = exportsObject
const row = { id: "one", pinned: false, folder: "", title: "Session" }
const tick = () => new Promise(resolve => setImmediate(resolve))
function harness() {
  const requests = []
  const updates = new SessionPreferenceUpdates((id, patch, signal) => new Promise((resolve, reject) => {
    requests.push({ id, patch, signal, resolve, reject })
  }), () => {})
  return { updates, requests }
}

test("preference edits render before saving and ignore reads begun before or during the save", async () => {
  const { updates, requests } = harness()
  const before = updates.checkpoint()
  const result = updates.update(row.id, { pinned: true })
  assert.equal(updates.apply(row).pinned, true)
  const during = updates.checkpoint()
  await tick()
  requests[0].resolve()
  await result
  for (const checkpoint of [before, during]) {
    updates.acceptRead(checkpoint, [row])
    assert.equal(updates.apply(row).pinned, true)
  }
  updates.acceptRead(updates.checkpoint(), [{ ...row, pinned: true }])
  assert.equal(updates.apply({ ...row, pinned: true }).pinned, true)
  assert.equal(updates.apply(row).pinned, false, "later authoritative changes remain visible")
})

test("filtered reads and legacy ID collisions do not retire a native session's saved preference", async () => {
  const { updates, requests } = harness()
  const result = updates.update(row.id, { pinned: true })
  await tick(); requests[0].resolve(); await result
  for (const rows of [[], [{ ...row, id: "another-session" }], [{ ...row, source: "legacy" }], [{ ...row, read_only: true }]]) {
    updates.acceptRead(updates.checkpoint(), rows)
    assert.equal(updates.apply(row).pinned, true)
  }
  updates.acceptRead(updates.checkpoint(), [{ ...row, pinned: true }])
  assert.equal(updates.apply(row).pinned, false, "a fresh read of this native session can retire its overlay")
})

test("rapid changes serialize per session while other sessions can save independently", async () => {
  const { updates, requests } = harness()
  const a = updates.update("one", { pinned: true })
  const b = updates.update("one", { folder: "Today" })
  const c = updates.update("one", { pinned: false })
  const d = updates.update("two", { pinned: true })
  assert.deepEqual(updates.apply(row), { ...row, folder: "Today" })
  await tick()
  assert.deepEqual(requests.map(item => item.id), ["one", "two"])
  requests[0].resolve(); requests[1].resolve()
  await Promise.all([a, d]); await tick()
  assert.deepEqual(requests[2].patch, { folder: "Today" })
  requests[2].resolve()
  await b; await tick()
  assert.deepEqual(requests[3].patch, { pinned: false })
  requests[3].resolve()
  await c
  assert.deepEqual(updates.apply(row), { ...row, folder: "Today" })
})

test("failed edits roll back only their own fields, preserving successful and newer intent", async () => {
  const { updates, requests } = harness()
  const pin = updates.update("one", { pinned: true })
  await tick(); requests[0].resolve(); await pin
  const badMove = updates.update("one", { folder: "Removed" })
  const failed = assert.rejects(badMove, /Folder not found/)
  const newMove = updates.update("one", { folder: "Today" })
  await tick(); requests[1].reject(new Error("Folder not found"))
  await failed; await tick()
  assert.deepEqual(updates.apply(row), { ...row, pinned: true, folder: "Today" })
  requests[2].resolve(); await newMove
  const unpin = updates.update("one", { pinned: false })
  const unpinFailed = assert.rejects(unpin, /Offline/)
  await tick(); requests[3].reject(new Error("Offline")); await unpinFailed
  assert.deepEqual(updates.apply(row), { ...row, pinned: true, folder: "Today" })
})

test("a failed first edit returns to the server row without masking unrelated fresh data", async () => {
  const { updates, requests } = harness()
  const result = updates.update("one", { folder: "Today" })
  const failed = assert.rejects(result)
  await tick(); requests[0].reject(new Error("Not saved")); await failed
  assert.deepEqual(updates.apply({ ...row, title: "New title" }), { ...row, title: "New title" })
})

test("sign-out or access changes discard overlays, abort writes and never send queued old-user edits", async () => {
  const { updates, requests } = harness()
  const first = updates.update("one", { pinned: true })
  const second = updates.update("one", { folder: "Today" })
  const failed = [assert.rejects(first, { name: "AbortError" }), assert.rejects(second, { name: "AbortError" })]
  await tick(); updates.clear()
  assert.equal(requests[0].signal.aborted, true)
  assert.deepEqual(updates.apply(row), row)
  requests[0].resolve()
  await Promise.all(failed)
  assert.equal(requests.length, 1)
})

test("native overlays never change read-only history with a colliding id", async () => {
  const { updates, requests } = harness()
  const result = updates.update("one", { pinned: true })
  for (const history of [{ ...row, source: "legacy" }, { ...row, read_only: true }])
    assert.deepEqual(updates.apply(history), history)
  await tick(); requests[0].resolve(); await result
})

test("only pin and folder patches use the optimistic preference path", () => {
  for (const value of [{}, { pinned: "yes" }, { folder: null }, { archived: true }, { title: "New", pinned: true }])
    assert.equal(preferencePatch(value), null)
  assert.deepEqual(preferencePatch({ pinned: false, folder: "" }), { pinned: false, folder: "" })
})
