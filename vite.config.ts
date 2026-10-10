import { defineConfig } from "vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

const frontend = resolve(__dirname, "frontend")

export default defineConfig({
  root: frontend,
  publicDir: false,
  plugins: [
    tanstackRouter({ target: "react", routesDirectory: resolve(frontend, "routes"), generatedRouteTree: resolve(frontend, "routeTree.gen.ts"), autoCodeSplitting: true }),
    tailwindcss(),
  ],
  resolve: { alias: { "@": frontend } },
  base: "/static/spa/",
  build: {
    outDir: resolve(__dirname, "app/static/spa"),
    emptyOutDir: true,
    // CSP allows only same-origin scripts, so assets are never inlined as data: URLs.
    assetsInlineLimit: 0,
    rollupOptions: {
      onwarn(warning, warn) { if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning) },
    },
  },
})
