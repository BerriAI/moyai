const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("../fixtures/frontend-imports.cjs")

const feature = path.join(web, "automations")
const generated = path.join(web, "generated")
const ui = path.join(root, "frontend/components/ui")
const logic = new Set(sources(feature).filter(file => !file.endsWith(".tsx")))
const transport = new Set(["api.ts", "api-client.ts"].map(name => path.join(web, name)))

test("nested automation TypeScript sources and helper declarations are typechecked", () => {
  assert.deepEqual(config.errors, [])
  const included = new Set(config.fileNames)
  for (const file of sources(feature).filter(file => /\.tsx?$/.test(file))) {
    assert.ok(included.has(file), `${file} is missing from tsconfig`)
  }
})

test("automation logic uses only its own logic and generated contracts", () => {
  for (const file of logic) {
    for (const dependency of imports(file)) {
      assert.ok(logic.has(dependency.file) || inside(dependency.file, generated),
        `${path.relative(root, file)} imports ${dependency.specifier}; keep rendering and effects outside automation logic`)
    }
  }
})

test("automation rendering uses its own modules, shared controls and transport", () => {
  for (const file of sources(feature).filter(file => !logic.has(file))) {
    for (const dependency of imports(file)) {
      assert.ok(["react", "lucide-react"].includes(dependency.specifier)
        || inside(dependency.file, feature) || inside(dependency.file, generated)
        || inside(dependency.file, ui) || transport.has(dependency.file),
      `${path.relative(root, file)} imports ${dependency.specifier}; keep the Settings shell and other features outside automations`)
    }
  }
})

test("shared transport, helpers and controls never depend on automations", () => {
  for (const file of [...shared, ...sources(generated), ...sources(ui)]) {
    for (const dependency of imports(file)) {
      assert.ok(!inside(dependency.file, feature),
        `${path.relative(root, file)} imports ${dependency.specifier}; shared code must not load a feature`)
    }
  }
})
