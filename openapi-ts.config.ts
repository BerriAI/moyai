import { defineConfig } from "@hey-api/openapi-ts"

export default defineConfig({
  input: "native/web/generated/openapi.json",
  output: "native/web/generated/api",
  plugins: [
    "@hey-api/typescript",
    "@hey-api/sdk",
    { name: "@hey-api/client-fetch", runtimeConfigPath: "./native/web/api-client", throwOnError: true },
  ],
})
