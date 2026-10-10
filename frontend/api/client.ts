import { MutationCache, QueryClient } from "@tanstack/react-query"
import { controllers } from "../legacy/controllers"
import { client } from "./generated/client.gen"

export const queryClient = new QueryClient({
  // Any write may change what the controllers cached for page navigation.
  mutationCache: new MutationCache({ onSettled: () => controllers.invalidate() }),
  defaultOptions: { queries: { staleTime: 15_000, retry: false, refetchOnWindowFocus: false } },
})
client.setConfig({ baseUrl: location.origin, credentials: "same-origin" })
client.interceptors.request.use(request => {
  request.headers.set("X-CSRF-Token", controllers.csrf())
  return request
})
client.interceptors.response.use(response => {
  if (response.status === 401 || response.status === 403) {
    // Keep observed and in-flight queries alive so their access error settles.
    // Identity changes clear the whole client in the runtime's ready handler.
    queryClient.removeQueries({ type: "inactive", predicate: query => query.state.fetchStatus === "idle" })
    controllers.invalidate()
  }
  return response
})

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export function unwrap<T>(result: { data?: T; error?: unknown; response?: Response }): T {
  if (result.response?.ok && result.data !== undefined) return result.data
  const error = result.error
  const detail = typeof error === "object" && error !== null && "detail" in error ? error.detail : null
  const status = result.response?.status ?? 0
  const fallback = status === 200 ? "The server returned an incompatible response. Reload the page or try again." : status ? `The request could not be completed (HTTP ${status}).` : "Could not connect to the server. Try again."
  throw new ApiError(typeof detail === "string" ? detail : fallback, status)
}
