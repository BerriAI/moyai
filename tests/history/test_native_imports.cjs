const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("../fixtures/frontend-imports.cjs")

const feature = path.join(web, "history")
const generated = path.join(web, "generated")
const ui = path.join(root, "frontend/components/ui")
const logic = new Set(sources(feature).filter(file => !file.endsWith(".tsx")))
const rendering = new Set(["api.ts", "messages/slack-content.tsx", "assets/slack-logo.svg"]
  .map(name => path.join(web, name)))

test("nested history TypeScript sources are typechecked", () => {
  assert.deepEqual(config.errors, [])
  const included = new Set(config.fileNames)
  for (const file of sources(feature).filter(file => /\.tsx?$/.test(file))) {
    assert.ok(included.has(file), `${file} is missing from tsconfig`)
  }
})

test("history route logic uses only its own logic and generated contracts", () => {
  for (const file of logic) {
    for (const dependency of imports(file)) {
      assert.ok(logic.has(dependency.file) || inside(dependency.file, generated),
        `${path.relative(root, file)} imports ${dependency.specifier}; keep rendering outside history route logic`)
    }
  }
})

test("history rendering uses its own modules, shared presentation and transport", () => {
  for (const file of sources(feature).filter(file => !logic.has(file))) {
    for (const dependency of imports(file)) {
      assert.ok(["react", "react-markdown", "remark-gfm", "lucide-react"].includes(dependency.specifier)
        || inside(dependency.file, feature) || inside(dependency.file, generated)
        || inside(dependency.file, ui) || rendering.has(dependency.file),
      `${path.relative(root, file)} imports ${dependency.specifier}; keep the application shell and other features outside history`)
    }
  }
})

test("shared transport, helpers and presentation never depend on history", () => {
  for (const file of new Set([...shared, ...sources(generated), ...sources(ui),
    path.join(web, "messages/slack-content.tsx")])) {
    for (const dependency of imports(file)) {
      assert.ok(!inside(dependency.file, feature),
        `${path.relative(root, file)} imports ${dependency.specifier}; shared code must not load a feature`)
    }
  }
})
