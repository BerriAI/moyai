import { defineConfig } from "@hey-api/openapi-ts"

export default defineConfig({
  input: "frontend/api/openapi.json",
  output: "frontend/api/generated",
  plugins: [
    "@hey-api/typescript",
    "@hey-api/client-fetch",
    { name: "zod", requests: true, responses: true },
    { name: "@hey-api/sdk", validator: true },
  ],
})
