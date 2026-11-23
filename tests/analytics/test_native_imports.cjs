const test = require("node:test")
const assert = require("node:assert/strict")
const path = require("node:path")
const { root, web, config, shared, inside, sources, imports } = require("../fixtures/frontend-imports.cjs")
const feature = path.join(web, "analytics")
const generated = path.join(web, "generated")
const ui = path.join(root, "frontend/components/ui")
const logic = new Set(sources(feature).filter(file => !file.endsWith(".tsx")))

test("analytics logic and declarations are included in typechecking", () => {
  assert.deepEqual(config.errors, [])
  for (const file of sources(feature).filter(file => /\.tsx?$/.test(file))) {
    assert.ok(config.fileNames.includes(file), `${file} is missing from tsconfig`)
  }
})

test("analytics arithmetic depends only on its logic and generated contracts", () => {
  for (const file of logic) for (const dependency of imports(file)) {
    assert.ok(logic.has(dependency.file) || inside(dependency.file, generated),
      `${path.relative(root, file)} imports ${dependency.specifier}; keep effects and rendering outside report arithmetic`)
  }
})

test("report rendering cannot load Settings or application composition", () => {
  for (const file of sources(feature).filter(file => !logic.has(file))) for (const dependency of imports(file)) {
    assert.ok(["react", "lucide-react", "html-react-parser"].includes(dependency.specifier)
      || inside(dependency.file, feature) || inside(dependency.file, generated)
      || inside(dependency.file, ui) || shared.has(dependency.file),
    `${path.relative(root, file)} imports ${dependency.specifier}; reports own rendering, not navigation`)
  }
})

test("shared transport and controls never depend on reports", () => {
  for (const file of [...shared, ...sources(generated), ...sources(ui)]) for (const dependency of imports(file)) {
    assert.ok(!inside(dependency.file, feature), `${path.relative(root, file)} imports a report`)
  }
})
