import { Generator, getConfig } from "@tanstack/router-generator"

const root = process.cwd()
const config = getConfig({
  target: "react",
  routesDirectory: "./frontend/routes",
  generatedRouteTree: "./frontend/routeTree.gen.ts",
  autoCodeSplitting: true,
}, root)

await new Generator({ config, root }).run()
