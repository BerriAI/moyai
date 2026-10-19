let csrf = ""
export const setCsrf = (value: string) => { csrf = value }
export const csrfToken = () => csrf

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = "Error" }
}

function detailMessage(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || !("detail" in value)) return
  const detail = value.detail
  if (typeof detail === "string" && detail.trim()) return detail
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg
}

function responseFailure(status: number): string {
  if (status === 401) return "Your session has expired. Please sign in again."
  if (status === 403) return "You no longer have access to this request."
  if (status >= 500) return `Moyai is temporarily unavailable (HTTP ${status}). Please try again.`
  return `The request could not be completed (HTTP ${status}). Please try again.`
}

/**
 * The single transport for /api: CSRF headers, one attempt, and ApiError on any
 * failure. Shared by the generated client (api-client.ts) and legacy api<T>().
 * One attempt only: callers must not silently replay a possibly accepted write.
 */
export async function send(input: string | Request, init: RequestInit = {}): Promise<Response> {
  const requestCsrf = csrf
  const signal = typeof input === "string" ? init.signal : input.signal
  const headers = typeof input === "string" ? new Headers(init.headers) : input.headers
  headers.set("X-Moyai-CSRF", requestCsrf)
  headers.set("X-CSRF-Token", requestCsrf)
  let response: Response
  try {
    response = await (typeof input === "string" ? fetch(input, { ...init, headers }) : fetch(input))
  } catch (error) {
    if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw error
    throw new ApiError("Could not reach Moyai. Check your connection and try again.", 0)
  }
  if (response.ok) return response
  let value: unknown
  try {
    value = await response.json()
  } catch (error) {
    if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw error
    // Error pages and empty error bodies must retain their HTTP status. Never
    // show their raw text/HTML (which may contain proxy or backend details).
  }
  if (response.status === 401 && csrf === requestCsrf) window.dispatchEvent(new Event("moyai:signed-out"))
  throw new ApiError(detailMessage(value) ?? responseFailure(response.status), response.status)
}

/** Legacy: `T` is an unchecked cast. New code calls the generated SDK from ./generated/api. */
export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  const response = await send(`/api${path}`, { ...options, headers })
  if (response.status === 204 || response.status === 205 || options.method === "HEAD") return undefined as T
  try {
    return await response.json() as T
  } catch (error) {
    if (options.signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw error
    throw new ApiError("Moyai returned an unreadable response. Please try again.", response.status)
  }
}

/** Legacy: see apiRequest. */
export function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  return apiRequest<T>(path, { method, signal, body: body === undefined ? undefined : JSON.stringify(body) })
}
