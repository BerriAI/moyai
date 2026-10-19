import type { CreateClientConfig } from "./generated/api/client.gen"
import { send } from "./api"

/**
 * Generated SDK calls share api.ts's transport: CSRF, one attempt, ApiError.
 * Calls throw ApiError on failure and resolve to `{ data }`. Paths already include /api.
 */
export const createClientConfig: CreateClientConfig = config => ({
  ...config,
  baseUrl: "",
  fetch: (input, init) => send(input instanceof Request ? input : String(input), init),
})
