import { normalizePath, type Plugin } from "vite"

/**
 * Expose the existing, declaration-only report helpers as a private ES module.
 * Ordinary legacy imports are unchanged; no bootstrap or globals are installed.
 */
export function legacyAnalyticsAdapter(sourcePath: string, formatModule: string): Plugin {
  const source = normalizePath(sourcePath)
  return {
    name: "moyai-legacy-analytics-module",
    enforce: "pre",
    transform(code, id) {
      const [path, query = ""] = id.split("?", 2)
      if (normalizePath(path) !== source || !new URLSearchParams(query).has("native-analytics")) return null
      return {
        code: `import { esc, dollars, spendCount } from ${JSON.stringify(normalizePath(formatModule))};\n${code}\nexport { analyticsDate, analyticsPreset, analyticsMetrics, analyticsLegend, analyticsChart, analyticsCSV };\n`,
        map: null,
      }
    },
  }
}
