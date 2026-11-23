const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("../fixtures/frontend-imports.cjs")
const feature = path.join(web, "conversations")

test("nested conversation sources are included in the TypeScript check", () => {
  assert.deepEqual(config.errors, [])
  const included = new Set(config.fileNames)
  for (const file of sources(feature)) assert.ok(included.has(file), `${file} is missing from tsconfig`)
})

test("conversation state depends only on its own modules, React and shared transport helpers", () => {
  for (const file of sources(feature)) {
    for (const dependency of imports(file)) {
      assert.ok(dependency.specifier === "react" || inside(dependency.file, feature)
        || inside(dependency.file, path.join(web, "generated")) || shared.has(dependency.file),
      `${path.relative(root, file)} imports ${dependency.specifier}; keep renderers and other features outside conversation state`)
    }
  }
})

test("shared transport, polling and upload modules never depend on conversation state", () => {
  for (const file of [...shared, ...sources(path.join(web, "generated"))]) {
    for (const dependency of imports(file)) {
      assert.ok(!inside(dependency.file, feature),
        `${path.relative(root, file)} imports ${dependency.specifier}; shared code must not load a feature`)
    }
  }
})
