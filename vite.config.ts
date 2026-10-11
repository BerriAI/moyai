import { defineConfig } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

export default defineConfig({
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  plugins: [tailwindcss()],
  resolve: { alias: { "@": resolve(__dirname, "frontend") } },
  build: {
    rollupOptions: { onwarn(warning, warn) { if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning) } },
    outDir: "app/static/ui",
    emptyOutDir: true,
    lib: { entry: "frontend/index.tsx", name: "MoyaiUI", formats: ["iife"], fileName: () => "moyai-ui.js", cssFileName: "moyai-ui" },
  },
})
