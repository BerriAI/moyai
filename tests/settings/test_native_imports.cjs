const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("../fixtures/frontend-imports.cjs")
const feature = path.join(web, "settings")
const generated = path.join(web, "generated")
const ui = path.join(root, "frontend/components/ui")
const shell = new Set(["brand.tsx", "appearance-toggle.tsx"].map(name => path.join(web, name)))

test("nested Settings sources are included in typechecking", () => {
  assert.deepEqual(config.errors, [])
  for (const file of sources(feature)) assert.ok(config.fileNames.includes(file), `${file} is missing from tsconfig`)
})

test("Settings composes its pages without importing the application entrypoint", () => {
  for (const file of sources(feature)) for (const dependency of imports(file)) {
    assert.ok(["react", "lucide-react"].includes(dependency.specifier)
      || inside(dependency.file, feature) || inside(dependency.file, generated)
      || inside(dependency.file, path.join(web, "analytics"))
      || inside(dependency.file, path.join(web, "automations"))
      || inside(dependency.file, ui) || shared.has(dependency.file) || shell.has(dependency.file),
    `${path.relative(root, file)} imports ${dependency.specifier}; keep workspace and application composition outside Settings`)
  }
})

test("preference contracts use generated types, and shared code cannot import Settings", () => {
  for (const dependency of imports(path.join(feature, "general-preferences.ts"))) {
    assert.ok(inside(dependency.file, generated), "Preference contracts must derive from the generated API")
  }
  for (const file of [...shared, ...sources(generated), ...sources(ui)]) for (const dependency of imports(file)) {
    assert.ok(!inside(dependency.file, feature), `${path.relative(root, file)} imports Settings`)
  }
})
