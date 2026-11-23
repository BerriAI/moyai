const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const ts = require("typescript")

const root = path.resolve(__dirname, "../..")
const web = path.join(root, "native/web")
const feature = path.join(web, "conversations")
const configFile = ts.readConfigFile(path.join(web, "tsconfig.json"), ts.sys.readFile)
const config = ts.parseJsonConfigFileContent(configFile.config, ts.sys, web)
const shared = new Set(["api.ts", "api-client.ts", "polling.ts", "attachment-uploads.ts", "attachment-files.ts"]
  .map(name => path.join(web, name)))
const inside = (file, directory) => file.startsWith(directory + path.sep)

function sources(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name)
    return entry.isDirectory() ? sources(file) : /\.tsx?$/.test(file) ? [file] : []
  })
}

function imports(file) {
  const source = ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true)
  const found = []
  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
      found.push(node.moduleSpecifier)
    } else if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword
      || ts.isIdentifier(node.expression) && node.expression.text === "require")) {
      assert.ok(node.arguments[0] && ts.isStringLiteral(node.arguments[0]), `${file}: use a literal module path`)
      found.push(node.arguments[0])
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
      found.push(node.argument.literal)
    } else if (ts.isExternalModuleReference(node)) {
      found.push(node.expression)
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found.map(node => {
    assert.ok(ts.isStringLiteral(node), `${file}: use a literal module path`)
    const resolved = ts.resolveModuleName(node.text, file, config.options, ts.sys).resolvedModule
    assert.ok(resolved, `${file}: cannot resolve ${node.text}`)
    return { specifier: node.text, file: resolved.resolvedFileName }
  })
}

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
