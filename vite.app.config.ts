import { defineConfig } from "vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import tailwindcss from "@tailwindcss/vite"
import { resolve } from "node:path"

export default defineConfig({
  plugins: [
    tanstackRouter({ target: "react", routesDirectory: "./frontend/routes", generatedRouteTree: "./frontend/routeTree.gen.ts", autoCodeSplitting: true }),
    tailwindcss(),
  ],
  resolve: { alias: { "@": resolve(__dirname, "frontend") } },
  base: "/static/spa/",
  build: {
    outDir: "app/static/spa",
    emptyOutDir: true,
    rollupOptions: {
      input: "frontend/main.tsx",
      output: { entryFileNames: "app.js", chunkFileNames: "[name]-[hash].js", assetFileNames: "[name][extname]" },
    },
  },
})
