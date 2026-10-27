import { defineConfig } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

export default defineConfig({
  base: "./",
  root: __dirname,
  publicDir: resolve(__dirname, "public"),
  plugins: [
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
