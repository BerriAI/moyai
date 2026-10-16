import { defineConfig } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"
import { legacyAnalyticsAdapter } from "./analytics-build"
import { legacyAutomationAdapter } from "./automation-build"

export default defineConfig({
  root: __dirname,
  publicDir: resolve(__dirname, "../../app/static/models"),
  plugins: [
    legacyAnalyticsAdapter(resolve(__dirname, "../../app/static/analytics.js"), resolve(__dirname, "analytics-format.ts")),
    legacyAutomationAdapter(resolve(__dirname, "../../app/static/automation-triggers.js"), resolve(__dirname, "../../app/static/automations.js")),
    tailwindcss(),
  ],
  resolve: { alias: { "@": resolve(__dirname, "../../frontend") } },
  build: {
    license: { fileName: "assets/licenses.md" },
    outDir: "../src/moyai/static",
    emptyOutDir: true,
    rollupOptions: {
      onwarn(warning, warn) { if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning) },
    },
  },
})
