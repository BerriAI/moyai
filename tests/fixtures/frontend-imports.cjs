const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const ts = require("typescript")

const root = path.resolve(__dirname, "../..")
const web = path.join(root, "native/web")
const configFile = ts.readConfigFile(path.join(web, "tsconfig.json"), ts.sys.readFile)
const config = ts.parseJsonConfigFileContent(configFile.config, ts.sys, web)
const shared = new Set(["api.ts", "api-client.ts", "polling.ts", "attachment-uploads.ts", "attachment-files.ts"]
  .map(name => path.join(web, name)))
const inside = (file, directory) => file.startsWith(directory + path.sep)

function sources(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name)
    return entry.isDirectory() ? sources(file) : /\.[jt]sx?$/.test(file) ? [file] : []
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
    if (/\.(css|svg)$/.test(node.text)) {
      const asset = path.resolve(path.dirname(file), node.text)
      assert.ok(fs.existsSync(asset), `${file}: cannot resolve ${node.text}`)
      return { specifier: node.text, file: asset }
    }
    const resolved = ts.resolveModuleName(node.text, file, config.options, ts.sys).resolvedModule
    assert.ok(resolved, `${file}: cannot resolve ${node.text}`)
    return { specifier: node.text, file: resolved.resolvedFileName }
  })
}

module.exports = { root, web, config, shared, inside, sources, imports }
