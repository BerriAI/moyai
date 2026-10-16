let csrf = ""
export const setCsrf = (value: string) => { csrf = value }
export const csrfToken = () => csrf

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = "Error" }
}

export async function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const requestCsrf = csrf
  const response = await fetch(`/api${path}`, {
    method, signal, headers: {
      "Content-Type": "application/json", "X-Moyai-CSRF": requestCsrf, "X-CSRF-Token": requestCsrf,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value = await response.json()
  if (!response.ok) {
    if (response.status === 401 && csrf === requestCsrf) window.dispatchEvent(new Event("moyai:signed-out"))
    throw new ApiError(typeof value.detail === "string" ? value.detail : Array.isArray(value.detail) && typeof value.detail[0]?.msg === "string" ? value.detail[0].msg : "The request could not be completed.", response.status)
  }
  return value
}
