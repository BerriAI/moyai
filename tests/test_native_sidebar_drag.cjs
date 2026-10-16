const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const test = require("node:test")
const ts = require("typescript")
const source = fs.readFileSync(path.join(__dirname, "../native/web/sidebar-drag.ts"), "utf8")
const exportsObject = {}
new Function("exports", ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
} }).outputText)(exportsObject)
const { sessionDrop, sessionPlacement, sessionCreatedByMe, currentDragSession, parseSessionDrag } = exportsObject
const id = "11111111-1111-4111-8111-111111111111"
const nonce = "22222222-2222-4222-8222-222222222222"
const row = { id, owner_id: "another-member", title: "Synthetic session", status: "idle", archived: false }
const folder = { id: "folder-id", name: "today" }
const drag = { version: 1, userId: "google:tester", sessionId: id, nonce }
const context = { userId: drag.userId, actionsUserId: drag.userId, view: "active", busy: false, sessions: [row], folders: [folder] }
const drop = (patch = {}, target = { kind: "folder", folderId: folder.id }, overrides = {}) =>
  sessionDrop(JSON.stringify(drag), drag, { ...context, sessions: [{ ...row, ...patch }], ...overrides }, target)

test("an internal current-user drag changes only the caller's folder preference, including shared sessions", () => {
  const result = drop({ pinned: true, unread: true, participated: true })
  assert.deepEqual(result.body, { folder: "today" })
  assert.equal(result.expand, folder.id)
  assert.equal(result.row.owner_id, "another-member")
  assert.equal(result.row.pinned, true)
  assert.equal(result.row.unread, true)
  assert.equal(result.row.participated, true)
})

test("folders take placement priority over pins, consistently with the existing folder-only Move action", () => {
  assert.deepEqual(sessionPlacement({ ...row, folder: "today", pinned: true }, [folder], drag.userId),
    { kind: "folder", key: folder.id, label: "today" })
  const result = drop({ folder: "today", pinned: true }, { kind: "unfiled" })
  assert.deepEqual(result.body, { folder: "" })
  assert.equal(result.expand, "pinned")
  assert.equal(result.message, "Moved to Pinned.")
  assert.equal(result.row.pinned, true)
})

test("dropping on any unfiled group clears the folder and derives participation without mutating it", () => {
  for (const participated of [true, false]) {
    const result = drop({ folder: "today", participated, owner_id: participated ? row.owner_id : drag.userId }, { kind: "unfiled" })
    assert.deepEqual(result.body, { folder: "" })
    assert.equal(result.expand, participated ? "participated" : "recent")
    assert.equal(result.row.participated, participated)
  }
})

test("stale, external, cross-account, and mismatched internal drag identities are refused", () => {
  const raw = JSON.stringify(drag)
  for (const active of [null, { ...drag, nonce: id }, { ...drag, userId: "other" }, { ...drag, sessionId: nonce }]) {
    assert.equal(sessionDrop(raw, active, context, { kind: "unfiled" }), null)
  }
  for (const override of [{ userId: "other" }, { actionsUserId: "other" }, { busy: true }, { view: "deleted" }, { sessions: [] }]) {
    assert.equal(drop({}, { kind: "unfiled" }, override), null)
  }
  assert.equal(drop({}, { kind: "folder", folderId: "removed-folder" }), null)
})

test("read-only history, deleted/deleting, unsupported identities and ambiguous session sources cannot move", () => {
  for (const patch of [
    { source: "legacy" }, { read_only: true }, { deleted: true }, { deleting: true },
    { source: "unknown" }, { id: "legacy-identifier" },
  ]) assert.equal(drop(patch), null)
  assert.equal(currentDragSession(drag, { ...context, sessions: [row, { ...row }] }), null)
  assert.ok(drop({ archived: true }, { kind: "unfiled" }, { view: "archived" }))
})

test("a rename during the drag uses the current folder ID/name; a no-op does not require a save", () => {
  const result = drop({}, { kind: "folder", folderId: folder.id }, { folders: [{ ...folder, name: "renamed" }] })
  assert.deepEqual(result.body, { folder: "renamed" })
  assert.equal(drop({ folder: "today" }).changed, false)
  assert.equal(drop({ pinned: true }, { kind: "unfiled" }).message, "Already in Pinned.")
})

test("malformed and oversized drag data is inert; unknown folder preferences remain visible without deletion", () => {
  for (const raw of ["", "{", "[]", "null", "x".repeat(2049),
    JSON.stringify({ ...drag, version: 2 }), JSON.stringify({ ...drag, userId: "" }),
    JSON.stringify({ ...drag, extra: "untrusted" }), JSON.stringify({ ...drag, nonce: "bad" })]) {
    assert.equal(parseSessionDrag(raw), null)
  }
  assert.equal(sessionPlacement({ ...row, folder: "unavailable", participated: true }, [folder], drag.userId).key, "participated")
  assert.equal(row.folder, undefined)
})

test("Recent requires ownership, including canonical linked identities; participation is other people's work", () => {
  assert.equal(sessionPlacement(row, [], drag.userId).kind, "hidden")
  assert.equal(sessionPlacement({ ...row, participated: true }, [], drag.userId).kind, "participated")
  assert.equal(sessionPlacement({ ...row, owner_id: drag.userId }, [], drag.userId).kind, "recent")
  const linked = { ...row, owner_id: "slack:team:creator", created_by_me: true }
  assert.equal(sessionCreatedByMe(linked, drag.userId), true)
  assert.equal(sessionPlacement(linked, [], drag.userId).kind, "recent")
  assert.equal(sessionPlacement({ ...linked, participated: true }, [], drag.userId).kind, "recent")
  assert.equal(sessionPlacement({ ...row, pinned: true }, [], drag.userId).kind, "pinned")
})

test("unfiling someone else's unparticipated session does not promise to move it to Recent", () => {
  const result = drop({ folder: "today" }, { kind: "unfiled" })
  assert.deepEqual(result.body, { folder: "" })
  assert.equal(result.expand, "")
  assert.equal(result.message, "Removed from folder.")
})
