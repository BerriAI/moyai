import { defineConfig } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

export default defineConfig({
  root: __dirname,
  publicDir: false,
  plugins: [tailwindcss()],
  resolve: { alias: { "@": resolve(__dirname, "../../frontend") } },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8798" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    license: { fileName: "assets/licenses.md" },
    rollupOptions: {
      onwarn(warning, warn) { if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning) },
    },
  },
})
