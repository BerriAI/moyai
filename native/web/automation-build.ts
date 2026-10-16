import ts from "typescript"
import { normalizePath, type Plugin } from "vite"

/** Extract only reviewed pure declarations, never the legacy controllers/bootstrap. */
export function automationDeclarations(code: string, names: readonly string[]): string {
  const source = ts.createSourceFile("automation-helpers.js", code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
  const found = new Map<string, string>()
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name && names.includes(statement.name.text)) {
      found.set(statement.name.text, statement.getText(source))
    } else if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && names.includes(declaration.name.text)) {
          found.set(declaration.name.text, `const ${declaration.getText(source)};`)
        }
      }
    }
  }
  for (const name of names) if (!found.has(name)) throw new Error(`Missing reviewed automation helper: ${name}`)
  return `${names.map(name => found.get(name)).join("\n")}\nexport { ${names.join(", ")} };\n`
}

export function legacyAutomationAdapter(triggerPath: string, listPath: string): Plugin {
  const sources = new Map([
    [normalizePath(triggerPath), ["automationDefaultSchedule", "automationTriggers", "automationSources", "automationEventFields", "automationSetupNotes"]],
    [normalizePath(listPath), ["automationTiming", "automationHistoryMerge", "automationRunStatus"]],
  ])
  return {
    name: "moyai-pure-automation-helpers",
    enforce: "pre",
    transform(code, id) {
      const [path, query = ""] = id.split("?", 2), names = sources.get(normalizePath(path))
      if (!names || !new URLSearchParams(query).has("native-automation")) return null
      return { code: automationDeclarations(code, names), map: null }
    },
  }
}
