import { defineConfig } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

export default defineConfig({
  root: __dirname,
  plugins: [tailwindcss()],
  resolve: { alias: { "@": resolve(__dirname, "../../frontend") } },
  build: {
    outDir: "../src/moyai/static",
    emptyOutDir: true,
    rollupOptions: {
      onwarn(warning, warn) { if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning) },
    },
  },
})
